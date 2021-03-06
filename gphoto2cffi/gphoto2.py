from __future__ import unicode_literals, division, absolute_import

import functools
import itertools
import logging
import math
import os
import re
import string
import sys
import time
from collections import namedtuple
from datetime import datetime
from io import BytesIO

from . import errors, backend
from .backend import ffi, lib
from .util import SimpleNamespace, get_string, get_ctype, new_gp_object

if sys.version_info > (3,):
    basestring = str


def get_library_version():
    """ Get the version number of the underlying gphoto2 library.

    :return: The version
    :rtype:  tuple of (major, minor, patch) version numbers
    """
    version_str = ffi.string(lib.gp_library_version(True)[0]).decode()
    return tuple(int(x) for x in version_str.split('.'))


def list_cameras(lazy=True):
    """ List all attached USB cameras that are supported by libgphoto2.

    :return:    All recognized cameras
    :rtype:     list of :py:class:`Camera`
    """
    ctx = lib.gp_context_new()
    camlist_p = new_gp_object("CameraList")
    port_list_p = new_gp_object("GPPortInfoList")
    lib.gp_port_info_list_load(port_list_p)
    abilities_list_p = new_gp_object("CameraAbilitiesList")
    lib.gp_abilities_list_load(abilities_list_p, ctx)
    lib.gp_abilities_list_detect(abilities_list_p, port_list_p,
                                 camlist_p, ctx)
    out = []
    for idx in range(lib.gp_list_count(camlist_p)):
        name = get_string(lib.gp_list_get_name, camlist_p, idx)
        value = get_string(lib.gp_list_get_value, camlist_p, idx)
        bus_no, device_no = (int(x) for x in
                             re.match(r"usb:(\d+),(\d+)", value).groups())
        abilities = ffi.new("CameraAbilities*")
        ability_idx = lib.gp_abilities_list_lookup_model(
            abilities_list_p, name.encode())
        lib.gp_abilities_list_get_abilities(abilities_list_p, ability_idx,
                                            abilities)
        if abilities.device_type == lib.GP_DEVICE_STILL_CAMERA:
            out.append(Camera(bus_no, device_no,
                              lazy=lazy,
                              _abilities=abilities))
    lib.gp_list_free(camlist_p)
    lib.gp_port_info_list_free(port_list_p)
    lib.gp_abilities_list_free(abilities_list_p)
    return out


def supported_cameras():
    """ List the names of all cameras supported by libgphoto2, grouped by the
    name of their driver.
    """
    ctx = lib.gp_context_new()
    abilities_list_p = new_gp_object("CameraAbilitiesList")
    lib.gp_abilities_list_load(abilities_list_p, ctx)
    abilities = ffi.new("CameraAbilities*")
    out = []
    for idx in range(lib.gp_abilities_list_count(abilities_list_p)):
        lib.gp_abilities_list_get_abilities(abilities_list_p, idx, abilities)
        if abilities.device_type == lib.GP_DEVICE_STILL_CAMERA:
            libname = os.path.basename(ffi.string(abilities.library)
                                       .decode())
            out.append((ffi.string(abilities.model).decode(), libname))
    lib.gp_abilities_list_free(abilities_list_p)
    key_func = lambda name, driver: driver
    out = sorted(out, key=key_func)
    return {k: tuple(x[0] for x in v)
            for k, v in itertools.groupby(out, key_func)}


def exit_after(meth=None, cam_struc=None):
    if meth is None:
        return functools.partial(exit_after, cam_struc=cam_struc)

    @functools.wraps(meth)
    def wrapped(self, *args, **kwargs):
        if not isinstance(self, Camera):
            cam, ctx = self._cam._cam, self._cam._ctx
        else:
            cam, ctx = self._cam, self._ctx
        try:
            rval = meth(self, *args, **kwargs)
        except Exception as e:
            print(str(e))
        else:
            return rval
        finally:
            lib.gp_camera_exit(cam, ctx)
    return wrapped


class Range(namedtuple("Range", ('min', 'max', 'step'))):
    """ Specifies a range of values (:py:attr:`max`, :py:attr:`min`,
        :py:attr:`step`)
    """
    pass


class ImageDimensions(namedtuple("ImageDimensions", ('width', 'height'))):
    """ Describes the dimension of an image (:py:attr:`width`,
        :py:attr:`height`)
    """
    pass


class UsbInformation(namedtuple(
    "UsbInformation", ('vendor', 'product', 'devclass', 'subclass',
                       'protocol'))):
    """ Information about a USB device. (:py:attr:`vendor`,
        :py:attr:`product`, :py:attr:`devclass`, :py:attr:`subclass`)
    """
    pass


class VideoCaptureContext(object):
    """ Context object that allows the stopping of a video capture via the
    :py:meth:`start` method.

    Can also be used as a context manager, where the capture will be stopped
    upon leaving. Get the resulting videofile by accessing the
    :py:attr:`videofile` attribute.

    """

    def __init__(self, camera):
        #: Camera the capture is running on
        self.camera = camera
        #: Resulting video :py:class:`File`, only available after stopping
        #: the capture
        self.videofile = None
        target = self.camera.config['settings']['capturetarget']
        self._old_captarget = target.value
        if self._old_captarget != "Memory card":
            target.set("Memory card")
        self.camera._get_config()['actions']['movie'].set(True)

    def stop(self):
        """ Stop the capture. """
        self.camera._get_config()['actions']['movie'].set(False)
        self.videofile = self.camera._wait_for_event(event_type=lib.GP_EVENT_FILE_ADDED)
        if self._old_captarget != "Memory card":
            self.camera.config['settings']['capturetarget'].set(
                self._old_captarget)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.videofile is None:
            self.stop()
        lib.gp_camera_exit(self.camera._cam, self.camera._ctx)


class Directory(object):
    """ A directory on the camera. """

    def __init__(self, name, parent, camera):
        self.name = name
        self.parent = parent
        self._file_ops = camera._abilities.file_operations
        self._dir_ops = camera._abilities.folder_operations
        self._cam = camera

    @property
    def path(self):
        """ Absolute path to the directory on the camera's filesystem. """
        if self.parent is None:
            return "/"
        else:
            return os.path.join(self.parent.path, self.name)

    @property
    def supported_operations(self):
        """ All directory operations supported by the camera. """
        return tuple(op for op in backend.DIR_OPS if self._dir_ops & op)

    @property
    def exists(self):
        """ Check whether the directory exists on the camera. """
        if self.name in ("", "/") and self.parent is None:
            return True
        else:
            return self in self.parent.directories

    @property
    @exit_after
    def files(self):
        """ Get a generator that yields all files in the directory. """
        filelist_p = new_gp_object("CameraList")
        lib.gp_camera_folder_list_files(self._cam._cam, self.path.encode(),
                                        filelist_p, self._cam._ctx)
        for idx in range(lib.gp_list_count(filelist_p)):
            fname = get_string(lib.gp_list_get_name, filelist_p, idx)
            yield File(filename=fname, directory=self, camera=self._cam)
        lib.gp_list_free(filelist_p)

    @property
    @exit_after
    def directories(self):
        """ Get a generator that yields all subdirectories in the directory.
        """
        dirlist_p = new_gp_object("CameraList")
        lib.gp_camera_folder_list_folders(self._cam._cam, self.path.encode(),
                                          dirlist_p, self._cam._ctx)
        for idx in range(lib.gp_list_count(dirlist_p)):
            name = os.path.join(
                self.path, get_string(lib.gp_list_get_name, dirlist_p, idx))
            yield Directory(name=name, parent=self, camera=self._cam)
        lib.gp_list_free(dirlist_p)

    @exit_after
    def create(self):
        """ Create the directory. """
        lib.gp_camera_folder_make_dir(
            self._cam._cam, self.parent.path.encode(), self.name.encode(),
            self._cam._ctx)

    @exit_after
    def remove(self):
        """ Remove the directory. """
        lib.gp_camera_folder_remove_dir(
            self._cam._cam, self.parent.path.encode(), self.name.encode(),
            self._cam._ctx)

    @exit_after
    def upload(self, local_path):
        """ Upload a file to the camera's permanent storage.

        :param local_path: Path to file to copy
        :type local_path:  str/unicode
        """
        camerafile_p = ffi.new("CameraFile**")
        with open(local_path, 'rb') as fp:
            lib.gp_file_new_from_fd(camerafile_p, fp.fileno())
            lib.gp_camera_folder_put_file(
                self._cam._cam, self.path.encode() + b"/",
                os.path.basename(local_path).encode(),
                backend.FILE_TYPES['normal'], camerafile_p[0],
                self._cam.ctx)

    def __eq__(self, other):
        return (self.name == other.name and
                self.parent == other.parent and
                self._cam == other._cam)

    def __repr__(self):
        return "Directory(\"{0}\")".format(self.path)


class File(object):
    """ A file on the camera. """

    def __init__(self, filename, directory, camera, ftype="normal"):
        self._filename = filename
        self._directory = directory
        self._cam = camera
        self.ftype = ftype
        self._operations = camera._abilities.file_operations
        self._readable = False
        self._file_bytes = BytesIO()
        self.camfile_p = \
            self.data_p = \
            self.length_p = \
            self.__info = None

    @property
    def supported_operations(self):
        """ All file operations supported by the camera. """
        return tuple(op for op in backend.FILE_OPS if self._operations & op)

    @property
    def size(self):
        """ File size in bytes.

        :rtype: int
        """
        return int(self._info.file.size)

    @property
    def readable(self):
        return self._readable

    @property
    def filename(self):
        return self._filename

    @property
    def mimetype(self):
        """ MIME type of the file.

        :rtype: str
        """
        return ffi.string(self._info.file.type).decode()

    @property
    def dimensions(self):
        """ Dimensions of the image.

        :rtype: :py:class:`ImageDimensions`
        """
        return ImageDimensions(self._info.file.width, self._info.file.height)

    @property
    def permissions(self):
        """ Permissions of the file.

        Can be "r-" (read-only), "-w" (write-only), "rw" (read-write)
        or "--" (no rights).

        :rtype: str
        """
        can_read = self._info.file.permissions & lib.GP_FILE_PERM_READ
        can_write = self._info.file.permissions & lib.GP_FILE_PERM_DELETE
        return "{0}{1}".format("r" if can_read else "-",
                               "w" if can_write else "-")

    @property
    def last_modified(self):
        """ Date of last modification.

        :rtype: :py:class:`datetime.datetime`
        """
        return datetime.fromtimestamp(self._info.file.mtime)

    @property
    def closed(self):
        if self._file_bytes:
            return self._file_bytes.closed
        else:
            raise OSError("File not opened")

    @property
    def exif(self):
        """
        Exif bytes for the file. Haven't worked out how to make any use of it though.
        :return: bytes
        """
        try:
            with File(self._filename, self._directory, self._cam, ftype="exif") as f:
                return f.open()
        except errors.GPhoto2Error as e:
            return None

    @property
    def raw(self):
        """
        Raw bayer data for the image.
        :return: bytes
        """
        try:
            with File(self._filename, self._directory, self._cam, ftype="raw") as f:
                return f.open()
        except errors.GPhoto2Error as e:
            return None

    @property
    def metadata(self):
        """
        metadata for the file.
        :return: bytes
        """
        try:
            with File(self._filename, self._directory, self._cam, ftype="metadata") as f:
                return f.open()
        except errors.GPhoto2Error as e:
            return None

    @property
    def audio(self):
        """
        audio for the file, will most likely only be useful for movies.
        :return: bytes
        """
        try:
            with File(self._filename, self._directory, self._cam, ftype="audio") as f:
                return f.open()
        except errors.GPhoto2Error as e:
            return None

    @exit_after
    def save(self, target_path=None, ftype='normal'):
        """ Save file content to a local file.
        :param target_path: Path to save remote file as, otherwise current dir with camera filename
        :type target_path:  str/unicode or None
        :type ftype:        str
        """
        if not target_path:
            target_path = self._filename

        self.open()
        self.seek(0)
        with open(target_path, 'wb') as fp:
            fp.write(self.read())
        self.seek(0)

    @exit_after
    def remove(self):
        """ Remove file from device. """
        lib.gp_camera_file_delete(self._cam._cam, self._directory.path.encode(),
                                  self._filename.encode(), self._cam._ctx)

    def remove_noexit(self):
        """ Remove file from device without shutting down connection. """
        lib.gp_camera_file_delete(self._cam._cam, self._directory.path.encode(),
                                  self._filename.encode(), self._cam._ctx)

    def open(self):
        """
        reads image from the camera to the internal BytesIO object.
        :rtype: :py:class:`File`
        """
        if self._readable:
            # dont open if already open, and seek to start.
            return self

        self.camfile_p = ffi.new("CameraFile**")
        self.data_p = ffi.new("char**")
        self.length_p = ffi.new("unsigned long*")
        lib.gp_file_new(self.camfile_p)
        lib.gp_camera_file_get(
            self._cam._cam, self._directory.path.encode(), self._filename.encode(),
            backend.FILE_TYPES[self.ftype], self.camfile_p[0], self._cam._ctx)

        lib.gp_file_get_data_and_size(self.camfile_p[0], self.data_p, self.length_p)
        # make sure that internal BytesIO exises and is clear
        self._file_bytes = BytesIO()
        self._file_bytes.write(bytes(ffi.buffer(self.data_p[0], self.length_p[0])))
        self._file_bytes.seek(0)
        try:
            self.__info = ffi.new("CameraFileInfo*")
            lib.gp_camera_file_get_info(
                self._cam._cam, self._directory.path.encode(),
                self._filename.encode(), self.__info, self._cam._ctx)
        except errors.GPhoto2Error:
            # dont let silly gphoto2 errors stop us from opening
            pass
        self._readable = True
        return self

    def free_file(self):
        # keep track of the camfile_p
        if self.camfile_p:
            lib.gp_file_free(self.camfile_p[0])
            self.camfile_p = None

    def close(self):
        """
        Releases the gphoto camera file.
        Closes BytesIO.
        :return:
        """
        if self._readable:
            # gphoto2 CameraFile MUST be freed
            self.free_file()
            # just to be safe.
            del self.data_p, self.length_p
            self.data_p = self.length_p = None
            # close the bytesio
            self._file_bytes.close()
            self._readable = False

    def bytes_copy(self):
        """
        returns a copy of BytesIO of the image.
        :return: BytesIO
        """
        if self._readable:
            return BytesIO(self._file_bytes.read())
        else:
            raise OSError("File not opened")

    @exit_after
    @property
    def bytestring(self):
        """ Get file content as bytes.
        :return:            File content
        :rtype:             bytes
        """
        self.seek(0)
        return self.read()

    @exit_after
    def iter_bytestring(self, chunk_size=2 ** 16, ftype='normal'):
        """ Get an iterator that yields chunks of the file content.
        :param chunk_size:  Size of yielded chunks in bytes
        :type chunk_size:   int
        :param ftype:       Select 'view' on file.
        :type ftype:        str
        :return:            Iterator
        """
        # TODO: _check_type_supported doesnt exist, so this method always fails.
        self._check_type_supported(ftype)
        buf_p = ffi.new("char[{0}]".format(chunk_size))
        size_p = ffi.new("uint64_t*")
        offset_p = ffi.new("uint64_t*")
        for chunk_idx in range(int(math.ceil(self.size / chunk_size))):
            size_p[0] = chunk_size
            lib.gp_camera_file_read(
                self._cam._cam, self._directory.path.encode(),
                self._filename.encode(), backend.FILE_TYPES[ftype], offset_p[0],
                buf_p, size_p, self._cam._ctx)
            yield ffi.buffer(buf_p, size_p[0])[:]

    @property
    def _info(self):
        if self.__info is None:
            self.__info = ffi.new("CameraFileInfo*")
            try:
                lib.gp_camera_file_get_info(
                    self._cam._cam, self._directory.path.encode(),
                    self._filename.encode(), self.__info, self._cam._ctx)
                lib.gp_camera_exit(self._cam._cam, self._cam._ctx)
            except errors.GPhoto2Error:
                raise ValueError("Could not get file info, are you sure the "
                                 "file exists on the device?")
        return self.__info

    def read(self, *args, **kwargs):
        if self._readable:
            return self._file_bytes.read(*args, **kwargs)
        else:
            raise OSError("File not opened")

    def seek(self, *args, **kwargs):
        if self._readable:
            return self._file_bytes.seek(*args, **kwargs)
        else:
            raise OSError("File not opened")

    def tell(self):
        if self._readable:
            return self._file_bytes.tell()
        else:
            raise OSError("File not opened")

    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def __eq__(self, other):
        return (self._filename == other.name and
                self._directory == other.directory and
                self._cam == other._cam)

    def __repr__(self):
        return "File(\"{0}/{1}\")".format(self._directory.path.rstrip("/"),
                                          self._filename)


class ConfigItem(object):
    """ A configuration option on the device. """

    def __init__(self, widget, camera):
        self._widget = widget
        root_p = ffi.new("CameraWidget**")
        lib.gp_widget_get_root(self._widget, root_p)
        self._root = root_p[0]
        self._cam = camera
        #: Short name
        self.name = get_string(lib.gp_widget_get_name, widget)
        typenum = get_ctype("CameraWidgetType*", lib.gp_widget_get_type,
                            widget)
        #: Type of option, can be one of `selection`, `text`, `range`,
        #: `toggle` or `date`.
        self.type = backend.WIDGET_TYPES[typenum]
        #: Human-readable label
        self.label = get_string(lib.gp_widget_get_label, widget)
        #: Information about the widget
        self.info = get_string(lib.gp_widget_get_info, widget)
        #: Current value
        self.value = None

        value_fn = lib.gp_widget_get_value
        if self.type in ('selection', 'text'):
            self.value = get_string(value_fn, widget)
        elif self.type == 'range':
            self.value = get_ctype("float*", value_fn, widget)
            #: Valid range for value, only present when :py:attr:`type` is
            #: `range`.
            self.range = self._read_range()
        elif self.type in ('toggle', 'date'):
            val = get_ctype("int*", value_fn, widget)
            if self.type == 'date':
                self.value = val
            else:
                self.value = None if val == 2 else bool(val)
        else:
            raise ValueError("Unsupported widget type for ConfigItem: {0}"
                             .format(self.type))
        if self.type == 'selection':
            #: Valid choices for value, only present when :py:attr:`type`
            #: is `selection`.
            self.choices = self._read_choices()
        #: Whether the value can be written to or not
        self.readonly = bool(get_ctype(
            "int*", lib.gp_widget_get_readonly, widget))

    @exit_after
    def set(self, value):
        """ Update value of the option.

        Only possible for options with :py:attr:`readonly` set to `False`.
        If :py:attr:`type` is `choice`, the value must be one of the
        :py:attr:`choices`.
        If :py:attr:`type` is `range`, the value must be in the range
        described by :py:attr:`range`.

        :param value:   Value to set
        """
        if self.readonly:
            raise ValueError("Option is read-only.")
        val_p = None
        if self.type == 'selection':
            if value not in self.choices:
                raise ValueError("Invalid choice (valid: {0})".format(
                    repr(self.choices)))
            val_p = ffi.new("const char[]", value.encode())
        elif self.type == 'text':
            if not isinstance(value, basestring):
                raise ValueError("Value must be a string.")
            val_p = ffi.new("char**")
            val_p[0] = ffi.new("char[]", value.encode())
        elif self.type == 'range':
            if value < self.range.min or value > self.range.max:
                raise ValueError("Value exceeds valid range ({0}-{1}."
                                 .format(self.range.min, self.range.max))
            if value % self.range.step:
                raise ValueError("Value can only be changed in steps of {0}."
                                 .format(self.range.step))
            val_p = ffi.new("float*")
            val_p[0] = value
        elif self.type == 'toggle':
            if not isinstance(value, bool):
                raise ValueError("Value must be bool.")
            val_p = ffi.new("int*")
            val_p[0] = int(value)
        elif self.type == 'date':
            val_p = ffi.new("int*")
            val_p[0] = value
        lib.gp_widget_set_value(self._widget, val_p)
        lib.gp_camera_set_config(self._cam._cam, self._root, self._cam._ctx)

    def _read_choices(self):
        if self.type != 'selection':
            raise ValueError("Can only read choices for items of type "
                             "'selection'.")
        choices = []
        for idx in range(lib.gp_widget_count_choices(self._widget)):
            choices.append(get_string(lib.gp_widget_get_choice, self._widget,
                                      idx))
        return choices

    def _read_range(self):
        rmin = ffi.new("float*")
        rmax = ffi.new("float*")
        rinc = ffi.new("float*")
        lib.gp_widget_get_range(self._widget, rmin, rmax, rinc)
        return Range(rmin[0], rmax[0], rinc[0])

    def __repr__(self):
        return ("ConfigItem('{0}', {1}, {2}, r{3})"
                .format(self.label, self.type, repr(self.value),
                        "o" if self.readonly else "w"))


class Camera(object):
    """ A camera device.

    The specific device can be auto-detected or set manually by
    specifying the USB bus and device number.

    :param bus:         USB bus number
    :param device:      USB device number
    :param lazy:        Only initialize the device when needed
    """

    def __init__(self, bus=None, device=None, lazy=False, _abilities=None):
        self._logger = logging.getLogger()
        # NOTE: It is not strictly neccessary to create a context for every
        #       device, however it is significantly (>500ms) faster when
        #       actions are to be performed simultaneously.
        self._ctx = lib.gp_context_new()
        self._usb_address = (bus, device)
        self.__abilities = _abilities
        self.__cam = None
        if not lazy:
            # Trigger the property
            self._cam

    @exit_after
    def release(self):
        self._logger.debug("Releasing Camera")

    @property
    def supported_operations(self):
        """ All operations supported by the camera. """
        return tuple(op for op in backend.CAM_OPS
                     if self._abilities.operations & op)

    @property
    def usb_info(self):
        """ The camera's USB information. """
        return UsbInformation(self._abilities.usb_vendor,
                              self._abilities.usb_product,
                              self._abilities.usb_class,
                              self._abilities.usb_subclass,
                              self._abilities.usb_protocol)

    @property
    def model_name(self):
        """ Camera model name as specified in the gphoto2 driver. """
        return ffi.string(self._abilities.model).decode()

    @property
    def config(self):
        """ Writeable configuration parameters.

        :rtype:     dict
        """
        config = self._get_config()
        return {section: {itm.name: itm for itm in config[section].values()
                          if not itm.readonly}
                for section in config
                if 'settings' in section or section == 'other'}

    @property
    def status(self):
        """ Status information (read-only).

        :rtype:     :py:class:`SimpleNamespace`
        """
        config = self._get_config()
        is_hex = lambda name: (len(name) == 4 and
                               all(c in string.hexdigits for c in name))
        out = SimpleNamespace()
        for sect in config:
            for itm in config[sect].values():
                if (itm.readonly or sect == 'status') and not is_hex(itm.name):
                    setattr(out, itm.name, itm.value)
        return out

    @property
    def filesystem(self):
        """ The camera's root directory. """
        return Directory(name="/", parent=None, camera=self)

    @property
    @exit_after
    def storage_info(self):
        """ Information about the camera's storage. """
        info_p = ffi.new("CameraStorageInformation**")
        num_info_p = ffi.new("int*")
        lib.gp_camera_get_storageinfo(self._cam, info_p, num_info_p, self._ctx)
        infos = []
        for idx in range(num_info_p[0]):
            out = SimpleNamespace()
            struc = (info_p[0] + idx)
            fields = struc.fields
            if lib.GP_STORAGEINFO_BASE & fields:
                out.directory = next(
                    (d for d in self.list_all_directories()
                     if d.path == ffi.string(struc.basedir).decode()),
                    None)
            if lib.GP_STORAGEINFO_LABEL & fields:
                out.label = ffi.string(struc.label).decode()
            if lib.GP_STORAGEINFO_DESCRIPTION & fields:
                out.description = ffi.string(struc.description).decode()
            if lib.GP_STORAGEINFO_STORAGETYPE & fields:
                stype = struc.type
                if lib.GP_STORAGEINFO_ST_FIXED_ROM & stype:
                    out.type = 'fixed_rom'
                elif lib.GP_STORAGEINFO_ST_REMOVABLE_ROM & stype:
                    out.type = 'removable_rom'
                elif lib.GP_STORAGEINFO_ST_FIXED_RAM & stype:
                    out.type = 'fixed_ram'
                elif lib.GP_STORAGEINFO_ST_REMOVABLE_RAM & stype:
                    out.type = 'removable_ram'
                else:
                    out.type = 'unknown'
            if lib.GP_STORAGEINFO_ACCESS & fields:
                if lib.GP_STORAGEINFO_AC_READWRITE & struc.access:
                    out.access = 'read-write'
                elif lib.GP_STORAGEINFO_AC_READONLY & struc.access:
                    out.access = 'read-only'
                elif lib.GP_STORAGEINFO_AC_READONLY_WITH_DELETE & struc.access:
                    out.access = 'read-delete'
            if lib.GP_STORAGEINFO_MAXCAPACITY & fields:
                out.capacity = int(struc.capacitykbytes)
            if lib.GP_STORAGEINFO_FREESPACEKBYTES & fields:
                out.free_space = int(struc.freekbytes)
            if lib.GP_STORAGEINFO_FREESPACEIMAGES & fields:
                out.remaining_images = int(struc.freeimages)
            infos.append(out)
        return infos

    def list_all_files(self):
        """ Utility method that yields all files on the device's file
            systems.
        """

        def list_files_recursively(directory):
            f_gen = itertools.chain(
                directory.files,
                *tuple(list_files_recursively(d)
                       for d in directory.directories))
            for f in f_gen:
                yield f

        return list_files_recursively(self.filesystem)

    def list_all_directories(self):
        """ Utility method that yields all directories on the device's file
            systems.
        """

        def list_dirs_recursively(directory):
            if directory == self.filesystem:
                yield directory
            d_gen = itertools.chain(
                directory.directories,
                *tuple(list_dirs_recursively(d)
                       for d in directory.directories))
            for d in d_gen:
                yield d

        return list_dirs_recursively(self.filesystem)

    def capture_video_context(self):
        """ Get a :py:class:`VideoCaptureContext` object.

        This allows the user to control when to stop the video capture.

        :rtype:     :py:class:`VideoCaptureContext`
        """
        return VideoCaptureContext(self)

    @exit_after
    def capture_video(self, length):
        """ Capture a video.

        This always writes to the memory card, since internal RAM is likely
        to run out of space very quickly.

        Currently this only works with Nikon cameras.

        :param length:      Length of the video to capture in seconds.
        :type length:       int
        :return:            Video file
        :rtype:             :py:class:`File`
        """
        with self.capture_video_context() as ctx:
            time.sleep(length)
        return ctx.videofile

    @exit_after
    def get_preview(self):
        """ Get a preview from the camera's viewport.

        This will usually be a JPEG image with the dimensions depending on
        the camera.

        :return:    The preview image as a BytesIO
        :rtype:     BytesIO
        """
        camfile_p = ffi.new("CameraFile**")
        lib.gp_file_new(camfile_p)
        lib.gp_camera_capture_preview(self._cam, camfile_p[0], self._ctx)
        data_p = ffi.new("char**")
        length_p = ffi.new("unsigned long*")
        lib.gp_file_get_data_and_size(camfile_p[0], data_p, length_p)
        b = BytesIO(bytes(ffi.buffer(data_p[0], length_p[0])))
        lib.gp_file_free(camfile_p[0])
        return b

    @property
    def _cam(self):
        if self.__cam is None:
            self.__cam = new_gp_object("Camera")
            if self._usb_address != (None, None):
                port_name = ("usb:{0:03},{1:03}".format(*self._usb_address)
                             .encode())
                port_list_p = new_gp_object("GPPortInfoList")
                lib.gp_port_info_list_load(port_list_p)
                port_info_p = ffi.new("GPPortInfo*")
                lib.gp_port_info_new(port_info_p)
                port_num = lib.gp_port_info_list_lookup_path(
                    port_list_p, port_name)
                lib.gp_port_info_list_get_info(port_list_p, port_num,
                                               port_info_p)
                lib.gp_camera_set_port_info(self.__cam, port_info_p[0])
                lib.gp_camera_init(self.__cam, self._ctx)
            else:
                try:
                    lib.gp_camera_init(self.__cam, self._ctx)
                except errors.UnsupportedDevice as e:
                    raise errors.UnsupportedDevice(
                        e.error_code, "Could not find any supported devices.")
        return self.__cam

    @property
    def _abilities(self):
        if self.__abilities is None:
            self.__abilities = ffi.new("CameraAbilities*")
            lib.gp_camera_get_abilities(self._cam, self.__abilities)
        return self.__abilities

    @exit_after
    def capture(self, to_camera_storage=False, timeout=5, img_expect_count=-1):
        """ Capture images and yields them as they area added.


        :param to_camera_storage:   Save image to the camera's internal storage
        :type to_camera_storage:    bool
        :param img_expect_count:    number of images expected from the camera.
        :type img_expect_count:    int
        :param timeout:   how long to wait for images to be stored
        :type timeout:    int how long in seconds to wait
        :return:    Generator of :py:class:`File` if `to_camera_storage` was `True`,
                    otherwise the captured image as BytesIO.
        :rtype:     generator of :py:class:`File` or BytesIO
        """
        target = self.config['settings']['capturetarget']
        if to_camera_storage and target.value != "Memory card":
            target.set("Memory card")
        elif not to_camera_storage and target.value != "Internal RAM":
            target.set("Internal RAM")
        del target

        # This is the canonical way to capture, its the method used in the user
        # facing 'gphoto2' program

        # dont consume events!
        dirs = list(self.list_all_directories())

        # initial target file
        camfile_p = ffi.new("CameraFilePath*")
        result = lib.gp_camera_capture(self._cam, backend.CAPTURE_TYPES['capture_image'], camfile_p, self._ctx)
        # if result !=0:
        #     self._logger.error("Couldnt capture for some reason...")
        #     raise StopIteration
        dirname = str(ffi.string(camfile_p[0].folder).decode())
        directory = list(f for f in dirs if f.path == dirname)[0]
        name = str(ffi.string(camfile_p[0].name).decode())
        self._logger.info("Initial capture resulted in {}".format(name))
        # yield the first file if to_camera_storage:
        if to_camera_storage:
            yield File(filename=name, directory=directory, camera=self)
        else:
            f = File(filename=name, directory=directory, camera=self)
            yield f.open()
            f.free_file()
            try:
                # need to not shut down connection to camera
                # otherwise on the next iteration will break
                f.remove_noexit()
            except errors.CameraIOError:
                # removed from RAM
                pass
        # delete that file
        del camfile_p
        # we have 1 file now.
        img_count = 1

        # wait for the rest
        event_type_p = ffi.new("CameraEventType*")
        event_data_p = ffi.new("void**", ffi.NULL)
        # start timing
        start_time = time.time()

        # this loop is mostly identical to the one in _wait_for_event
        # for some reason it was causing the images to be deleted from ram
        # too quickly
        while True:
            # 200 is the lowest granularity to listen for events
            result = lib.gp_camera_wait_for_event(self._cam, 200,
                                         event_type_p,
                                         event_data_p,
                                         self._ctx)

            if event_type_p[0] == lib.GP_EVENT_FILE_ADDED:
                camfile_p = ffi.cast("CameraFilePath*", event_data_p[0])
                dirname = str(ffi.string(camfile_p[0].folder).decode())
                name = str(ffi.string(camfile_p[0].name).decode())
                directory = list(f for f in dirs if f.path == dirname)[0]
                self._logger.info("File added {}".format(name))
                # print("File added {}{}".format(dirname, name))
                img_count += 1
                if to_camera_storage:
                    yield File(filename=name, directory=directory, camera=self)
                else:
                    f = File(filename=name, directory=directory, camera=self)
                    yield f.open()
                    f.free_file()
                    try:
                        # need to not shut down connection to camera
                        # otherwise on the next iteration will break
                        f.remove_noexit()
                    except errors.CameraIOError:
                        # removed from RAM
                        pass
                del camfile_p
            elif event_type_p[0] == lib.GP_EVENT_CAPTURE_COMPLETE:
                # print("Capture complete.")
                self._logger.info("Capture complete.")
            elif event_type_p[0] == lib.GP_EVENT_TIMEOUT:
                # print("Timeout while waiting for event.")
                self._logger.debug("Timeout while waiting for event.")
            elif event_type_p[0] == lib.GP_EVENT_UNKNOWN:
                self._logger.debug("Unknown event.")
                pass

            if img_count >= img_expect_count > 0:
                break

            if event_type_p[0] == lib.GP_EVENT_CAPTURE_COMPLETE:
                break

            if timeout < time.time() - start_time:
                break
        del event_type_p
        del event_data_p
        # can safely release camera after now.

    @exit_after
    def trigger_capture_wait(self, to_camera_storage=False, timeout=10, img_expect_count=-1):
        """ Capture images and yields them as they area added.

        This method is recommended against in the gphoto2 code.
        We dont know how long to wait for the images, and some cameras do not
        fire FILE_ADDED when gp_camera_trigger_capture is called
        Some cameras (mostly Canon and Nikon) support capturing to internal
        RAM. On these devices, you have to specify `to_camera_storage` if
        you want to save the images to the memory card. On devices that
        do not support saving to RAM, the only difference is that the file
        is automatically downloaded and deleted when set to `False`.
        Some cameras do not fire lib.GP_EVENT_CAPTURE_COMPLETE, hence the timeout.

        :param to_camera_storage:   Save image to the camera's internal storage
        :type to_camera_storage:    bool
        :param img_expect_count:    number of images expected from the camera.
        :type img_expect_count:    int
        :param timeout:   how long to wait for images to be stored
        :type timeout:    int how long in seconds to wait
        :return:    Generator of :py:class:`File` if `to_camera_storage` was `True`,
                    otherwise the captured image as BytesIO.
        :rtype:     generator of :py:class:`File` or BytesIO
        """

        target = self.config['settings']['capturetarget']
        if to_camera_storage and target.value != "Memory card":
            target.set("Memory card")
        elif not to_camera_storage and target.value != "Internal RAM":
            target.set("Internal RAM")
        del target

        lib.gp_camera_trigger_capture(self._cam, self._ctx)
        fobjs = self._wait_for_event( event_type=lib.GP_EVENT_FILE_ADDED,
                                     exit_event_type=lib.GP_EVENT_CAPTURE_COMPLETE,
                                     img_expect_count=img_expect_count,
                                     timeout=timeout)
        if to_camera_storage:
            for f in fobjs:
                self._logger.info("File written to storage at {0}.".format(f))
                yield f
        else:

            for f in fobjs:
                # this downloads the file.
                yield f.open()
                f.free_file()
                try:
                    # need to not shut down connection to camera
                    # otherwise on the next iteration when the
                    f.remove_noexit()
                except errors.CameraIOError:
                    # removed from RAM
                    pass

    def _wait_for_event(self, event_type=None, exit_event_type=None, img_expect_count=-1, timeout=10):
        """
        waits for an event from libgphoto2
        primarily used for waiting for images to be written
        timeouts and exit_event_type is mainly for capturing both jpeg and raw files.
        :param event_type: event type to wait for,
        :param exit_event_type: event that causes us to stop waiting.
        :param img_expect_count: Number of images that are expected, if -1 keep waiting until timeout
        :param timeout: timeout in seconds for how long to wait on events. set to default of 20s
        :return: generator of :py:class:`File` if event_type is lib.GP_EVENT_FILE_ADDED
        """
        # having this within the loop consumes events for some reason.
        dirs = list(self.list_all_directories())
        if event_type is None and not timeout:
            raise ValueError("Please specifiy either `event_type` or `duration!`")

        # if exit_event_type not specified, only wait for the first event
        if event_type and not exit_event_type:
            exit_event_type = event_type

        # and vice versa
        if exit_event_type and not event_type:
            event_type = exit_event_type

        start_time = time.time()
        img_count = 0

        event_type_p = ffi.new("CameraEventType*")
        event_data_p = ffi.new("void**", ffi.NULL)

        while True:
            result = lib.gp_camera_wait_for_event(self._cam, 300,
                                         event_type_p,
                                         event_data_p,
                                         self._ctx)
            if event_type_p[0] == lib.GP_EVENT_FILE_ADDED and event_type == lib.GP_EVENT_FILE_ADDED:
                camfile_p = ffi.cast("CameraFilePath*", event_data_p[0])
                dirname = str(ffi.string(camfile_p[0].folder).decode())
                name = str(ffi.string(camfile_p[0].name).decode())
                directory = list(f for f in dirs if f.path == dirname)[0]
                self._logger.info("File added {}".format(name))
                # print("File added {}{}".format(dirname, name))
                img_count += 1
                yield File(filename=name, directory=directory, camera=self)
            elif event_type_p[0] == lib.GP_EVENT_CAPTURE_COMPLETE:
                # print("Capture complete.")
                self._logger.info("Capture complete.")
            elif event_type_p[0] == lib.GP_EVENT_TIMEOUT:
                # print("Timeout while waiting for event.")
                self._logger.debug("Timeout while waiting for event.")
            elif event_type_p[0] == lib.GP_EVENT_UNKNOWN:
                self._logger.debug("Unknown event.")
                pass
            if img_count >= img_expect_count > 0:
                break

            if event_type_p[0] == exit_event_type:
                break

            if timeout < time.time() - start_time:
                break

    @exit_after
    def _get_config(self):
        def _widget_to_dict(cwidget):
            out = {}
            for idx in range(lib.gp_widget_count_children(cwidget)):
                child_p = ffi.new("CameraWidget**")
                lib.gp_widget_get_child(cwidget, idx, child_p)
                key = get_string(lib.gp_widget_get_name, child_p[0])
                typenum = get_ctype("CameraWidgetType*",
                                    lib.gp_widget_get_type, child_p[0])
                if typenum in (lib.GP_WIDGET_WINDOW, lib.GP_WIDGET_SECTION):
                    out[key] = _widget_to_dict(child_p[0])
                else:
                    item = ConfigItem(child_p[0], self)
                    out[key] = item
            return out

        root_widget = ffi.new("CameraWidget**")
        lib.gp_camera_get_config(self._cam, root_widget, self._ctx)
        return _widget_to_dict(root_widget[0])

    def __repr__(self):
        return "<Camera \"{0}\" at usb:{1:03}:{2:03}>".format(
            self.model_name, *self._usb_address)

    def __del__(self):
        if self.__cam is not None:
            lib.gp_camera_exit(self.__cam, self._ctx)
            lib.gp_camera_free(self.__cam)
