from bpylist import bplist
from bpylist.archive_types import *
from typing import Mapping, List, Optional, Union, Iterator, IO
import json

# The magic number which Cocoa uses as an implementation version.
# I don' think there were 99_999 previous implementations, I think
# Apple just likes to store a lot of zeros
NSKeyedArchiveVersion = 100_000

# Cached for convenience
null_uid = uid(0)


def loads(plist: bytes, class_map: Union[None, Mapping[str, type], 'ClassMap'] = None, opaque=False) -> object:
    "Unpack an NSKeyedArchived byte blob into a more useful object tree."
    unarch = Unarchive(plist)
    if isinstance(class_map, ClassMap):
        unarch.class_map = class_map
    elif class_map is not None:
        unarch.class_map.update(class_map)
    if opaque:
        unarch.class_map = OpaqueClassMap(unarch.class_map)
    return unarch.top_object()


def load(f: IO[bytes], class_map: Union[None, Mapping[str, type], 'ClassMap'] = None, opaque=False) -> object:
    "Unpack a file-like object with NSKeyedArchived data into a more useful object tree."
    return loads(f.read(), class_map, opaque)


def dumps(obj: object, class_map: Union[None, Mapping[str, type], 'ClassMap'] = None, opaque=False) -> bytes:
    "Pack an object tree into an NSKeyedArchived blob."
    arch = Archive(obj)
    if isinstance(class_map, ClassMap):
        arch.class_map = class_map
    elif class_map is not None:
        arch.class_map.update(class_map)
    if opaque:
        arch.class_map = OpaqueClassMap(arch.class_map)
    return arch.to_bytes()


def dump(obj: object, f: IO[bytes], class_map: Union[None, Mapping[str, type], 'ClassMap'] = None, opaque=False):
    f.write(dumps(obj, class_map, opaque))


class ArchiverError(Exception):
    pass


class UnsupportedArchiver(ArchiverError):
    """
    Just in case we are given a regular NSArchive instead of an NSKeyedArchive,
    or if Apple introduces a new archiver and we are given some of its work.
    """

    def __init__(self, alternate):
        super().__init__(f"unsupported encoder: `{alternate}'")


class UnsupportedArchiveVersion(ArchiverError):
    def __init__(self, version):
        super().__init__(f"expected {NSKeyedArchiveVersion}, got `{version}'")


class MissingTopObject(ArchiverError):
    def __init__(self, plist):
        super().__init__(f"no top object! plist dump: {plist}")


class MissingTopObjectUID(ArchiverError):
    def __init__(self, top):
        super().__init__(f"top object did not have a UID! dump: {top}")


class MissingObjectsArray(ArchiverError):
    def __init__(self, plist):
        super().__init__(f"full plist dump: `{plist}'")


class MissingClassMetaData(ArchiverError):
    def __init__(self, index, result):
        super().__init__(f"$class had no metadata {index}: {result}")


class MissingClassName(ArchiverError):
    def __init__(self, meta):
        super().__init__(f"$class had no $classname; $class = {meta}")


class MissingClassList(ArchiverError):
    def __init__(self, meta):
        super().__init__(f"$class had no $classes; $class = {meta}")


class InvalidClassList(ArchiverError):
    def __init__(self, meta):
        super().__init__(f"$class first entry in $classes differs from $classname; $class = {meta}")


class MissingClassUID(ArchiverError):
    def __init__(self, obj):
        super().__init__(f"object has no $class: {obj}")


class CircularReference(ArchiverError):
    def __init__(self, index):
        super().__init__(f"archive has a cycle with {index}")


class MissingClassMapping(ArchiverError):
    def __init__(self, name, mapping):
        super().__init__(f"no mapping for {name} in {mapping}")


class ArchivedObject:
    """
    Stateful wrapper around Unarchive for an archived object.

    This is the object that will be passed to unarchiving delegates
    so that they can construct objects. The only useful method on
    this class is decode(self, key).
    """

    def __init__(self, uid, obj, unarchiver):
        self._uid = uid
        self._object = obj
        self._unarchiver = unarchiver

    def _decode_index(self, index: uid):
        return self._unarchiver.decode_object(index)

    def decode(self, key: str):
        return self._unarchiver.decode_key(self._object, key)

    def keys(self):
        return (k for k in self._object.keys() if k != '$class')


class Unarchive:
    """
    Capable of unpacking an archived object tree in the NSKeyedArchive format.

    Apple's implementation can be found here:
    https://github.com/apple/swift-corelibs-foundation/blob/master/Foundation\
    /NSKeyedUnarchiver.swift

    Note: At this time, we support only a limited subset of circular
    references. In general, cycles in the object tree being unarchived is
    be considered forbidden by this implementation.

    In order to properly support circular references, the unarchiver needs to
    separate allocation from initialization so that it can allocate an instance
    of a class and cache the reference before passing the instance to the
    decode-specific initializer. However, doing this for certain built-in types
    is non-trivial, and I don't want to have a mess of special cases.
    """

    def __init__(self, input: bytes):
        self.input = input
        self.class_map = DefaultClassMap()
        self.unpacked_uids = {}
        self.top_uid = null_uid
        self.objects = None

    def unpack_archive_header(self):
        plist = bplist.loads(self.input)

        archiver = plist.get('$archiver')
        if archiver != 'NSKeyedArchiver':
            raise UnsupportedArchiver(archiver)

        version = plist.get('$version')
        if version != NSKeyedArchiveVersion:
            raise UnsupportedArchiveVersion(version)

        top = plist.get('$top')
        if not isinstance(top, dict):
            raise MissingTopObject(plist)

        self.top_uid = top.get('root')
        if not isinstance(self.top_uid, uid):
            raise MissingTopObjectUID(top)

        self.objects = plist.get('$objects')
        if not isinstance(self.objects, list):
            raise MissingObjectsArray(plist)

    def class_for_uid(self, index: uid):
        "use the UNARCHIVE_CLASS_MAP to find the unarchiving delegate of a uid"

        meta = self.objects[index]
        if not isinstance(meta, dict):
            raise MissingClassMetaData(index, meta)

        name = meta.get('$classname')
        if not isinstance(name, str):
            raise MissingClassName(meta)

        classes = meta.get('$classes')
        if not isinstance(classes, list):
            raise MissingClassList(meta)

        if not classes or classes[0] != name:
            raise InvalidClassList(meta)

        klass = self.class_map.get_python_class(classes)
        if klass is None:
            raise MissingClassMapping(name, self.class_map)

        return klass

    def decode_key(self, obj, key):
        val = obj.get(key)
        if isinstance(val, uid):
            return self.decode_object(val)
        return val

    def decode_object(self, index: uid):
        # index 0 always points to the $null object, which is the archive's
        # special way of saying the value is null/nil/none
        if index == 0:
            return None

        obj = self.unpacked_uids.get(index)
        if obj is CycleToken:
            raise CircularReference(index)

        if obj is not None:
            return obj

        raw_obj = self.objects[index]

        # if obj is a (semi-)primitive type (e.g. str)
        if not isinstance(raw_obj, dict):
            return raw_obj

        class_uid = raw_obj.get('$class')
        if not isinstance(class_uid, uid):
            raise MissingClassUID(raw_obj)

        klass = self.class_for_uid(class_uid)

        # put a temp object in place, in case we have a circular reference
        # classes that don't support two-phase initialization should return CycleToken
        obj = klass.__new__(klass)
        self.unpacked_uids[index] = obj

        new_obj = klass.decode_archive(obj, ArchivedObject(uid, raw_obj, self))
        if obj is CycleToken:
            self.unpacked_uids[index] = new_obj
            return new_obj
        else:
            if new_obj is not None:
                print(klass)
            assert new_obj is None
            return obj

    def top_object(self):
        "recursively decode the root/top object and return the result"

        self.unpack_archive_header()
        return self.decode_object(self.top_uid)


class ArchivingObject:
    """
    Stateful wrapper around Archive for an object being archived.

    This is the object that will be passed to unarchiving delegates
    so that they can do their part in constructing the archive. The
    only useful method on this class is encode(self, key, val).
    """

    def __init__(self, archive_obj, archiver):
        self._archive_obj = archive_obj
        self._archiver = archiver

    def encode(self, key, val):
        val = self._archiver.encode(val)
        self._archive_obj[key] = val


class Archive:
    """
    Capable of packing an object tree into the NSKeyedArchive format.

    Apple's implementation can be found here:
    https://github.com/apple/swift-corelibs-foundation/blob/master/Foundation\
    /NSKeyedArchiver.swift

    Unlike our unarchiver, we are actually capable of archiving circular
    references...so, yeah.
    """

    # types which do not require the "object" encoding for an archive;
    primitive_types = [int, float, bool, str, bytes, uid]

    # types which require no extra encoding at all, they can be inlined
    # in the archive
    inline_types = [int, float, bool]

    def __init__(self, input):
        self.input = input
        self.class_map = DefaultClassMap()
        # cache/map class names (str) to uids
        self.class_cache = {}
        # cache/map of already archived objects to uids (to avoid cycles)
        self.ref_cache = {}
        # objects that go directly into the archive, always start with $null
        self.objects = ['$null']

    def uid_for_class_chain(self, class_chain: List[str]) -> uid:
        """
        Ensure the class definition for the archiver is included in the arcive.

        Non-primitive objects are encoded as a dictionary of key-value pairs;
        there is always a $class key, which has a UID value...the UID is itself
        a pointer/index which points to the definition of the class (which is
        also in the archive).

        This method makes sure that all the metadata is included in the archive
        exactly once (no duplicates class metadata).
        """

        val = self.class_cache.get(class_chain[0])
        if val:
            return val

        val = uid(len(self.objects))
        self.class_cache[class_chain[0]] = val
        self.objects.append({ '$classes': class_chain, '$classname': class_chain[0] })

        return val

    def encode(self, val):
        cls = val.__class__

        if cls in Archive.inline_types:
            return val

        return self.archive(val)

    def encode_list(self, objs, archive_obj):
        archiver_uid = self.uid_for_class_chain(['NSArray', 'NSObject'])
        archive_obj['$class'] = archiver_uid
        archive_obj['NS.objects'] = [self.archive(obj) for obj in objs]

    def encode_set(self, objs, archive_obj):
        archiver_uid = self.uid_for_class_chain(['NSSet', 'NSObject'])
        archive_obj['$class'] = archiver_uid
        archive_obj['NS.objects'] = [self.archive(obj) for obj in objs]

    def encode_dict(self, obj, archive_obj):
        archiver_uid = self.uid_for_class_chain(['NSDictionary', 'NSObject'])
        archive_obj['$class'] = archiver_uid

        keys = []
        vals = []
        for k in obj:
            keys.append(self.archive(k))
            vals.append(self.archive(obj[k]))

        archive_obj['NS.keys'] = keys
        archive_obj['NS.objects'] = vals

    def encode_top_level(self, obj, archive_obj):
        "Encode obj and store the encoding in archive_obj"

        cls = obj.__class__

        if cls == list:
            self.encode_list(obj, archive_obj)

        elif cls == dict:
            self.encode_dict(obj, archive_obj)

        elif cls == set:
            self.encode_set(obj, archive_obj)

        else:
            archiver = self.class_map.get_objc_class(cls)
            if archiver is None:
                raise MissingClassMapping(obj, self.class_map)

            archiver_uid = self.uid_for_class_chain(archiver)
            archive_obj['$class'] = archiver_uid

            archive_wrapper = ArchivingObject(archive_obj, self)
            cls.encode_archive(obj, archive_wrapper)

    def archive(self, obj) -> uid:
        "Add the encoded form of obj to the archive, returning the UID of obj."

        if obj is None:
            return null_uid

        # the ref_map allows us to avoid infinite recursion caused by
        # cycles in the object graph by functioning as a sort of promise
        ref = self.ref_cache.get(id(obj))
        if ref:
            return ref

        index = uid(len(self.objects))
        self.ref_cache[id(obj)] = index

        cls = obj.__class__
        if cls in Archive.primitive_types:
            self.objects.append(obj)
            return index

        archive_obj = {}
        self.objects.append(archive_obj)
        self.encode_top_level(obj, archive_obj)

        return index

    def to_bytes(self) -> bytes:
        "Generate the archive and return it as a bytes blob"

        # avoid regenerating
        if len(self.objects) == 1:
            self.archive(self.input)

        d = { '$archiver': 'NSKeyedArchiver',
              '$version': NSKeyedArchiveVersion,
              '$objects': self.objects,
              '$top': { 'root': uid(1) }
        }

        return bplist.dumps(d)


class ClassMap(object):
    def get_python_class(self, class_chain: List[str]) -> Optional[type]:
        return None

    def get_objc_class(self, cls: type) -> Optional[List[str]]:
        return None


class DefaultClassMap(ClassMap):
    def __init__(self):
        self.unarchive_class_map = {
            'NSDictionary':        Dict,
            'NSMutableDictionary': MutableDict,
            'NSArray':             Array,
            'NSMutableArray':      MutableArray,
            'NSSet':               Set,
            'NSMutableSet':        MutableSet,
            'NSMutableData':       MutableData,
            'NSDate':              timestamp_decoder,
        }
        self.archive_class_map = {
            dict: 'NSDictionary',
            MutableDict: 'NSMutableDictionary',
            list: 'NSArray',
            MutableArray: 'NSMutableArray',
            set: 'NSSet',
            MutableSet: 'NSMutableSet',
            MutableData: 'NSMutableData',
            timestamp: 'NSDate'
        }

    def get_python_class(self, class_chain):
        return self.unarchive_class_map.get(class_chain[0])

    def get_objc_class(self, cls):
        klass = self.archive_class_map.get(cls)
        if klass is None:
            return None
        mutable_prefix = 'NSMutable'
        immutable_prefix = 'NS'
        if klass.startswith(mutable_prefix):
            stem = klass[len(mutable_prefix):]
            return [klass, immutable_prefix + stem, 'NSObject']
        else:
            return [klass, 'NSObject']

    def update(self, new_map: Mapping[str, type]):
        self.unarchive_class_map.update(new_map)
        self.archive_class_map.update({v: k for k, v in new_map.items()})


class OpaqueClassMap(ClassMap):
    def __init__(self, base: ClassMap):
        self.base = base
        self.class_cache = {}

    def get_python_class(self, class_chain):
        k = self.base.get_python_class(class_chain)
        if k is not None:
            return k
        return self._make_class(iter(class_chain))

    def get_objc_class(self, cls):
        if issubclass(cls, OpaqueObject):
            return self._get_class_chain(cls)
        return self.base.get_objc_class(cls)

    def _make_class(self, class_chain_iter: Iterator[str]) -> type:
        try:
            objc_class = next(class_chain_iter)
        except StopIteration:
            return OpaqueObject
        klass = self.class_cache.get(objc_class)
        if klass is not None:
            return klass
        base = self._make_class(class_chain_iter)
        klass = type(objc_class, (base, ), {})
        self.class_cache[objc_class] = klass
        return klass

    def _get_class_chain(self, cls: type) -> List[str]:
        chain = []
        while cls is not OpaqueObject:
            chain.append(cls.__name__)
            cls = cls.__bases__[0]
        return chain


