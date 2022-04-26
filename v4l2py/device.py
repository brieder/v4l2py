#
# This file is part of the v4l2py project
#
# Copyright (c) 2021 Tiago Coutinho
# Distributed under the GPLv3 license. See LICENSE for more info.

import os
import enum
import mmap
import errno
import fcntl
import select
import pathlib
import collections

from . import raw


def _enum(name, prefix, klass=enum.IntEnum):
    return klass(name,
        ((name.replace(prefix, ""), getattr(raw, name))
        for name in dir(raw) if name.startswith(prefix)))


Capability = _enum("Capability", "V4L2_CAP_", klass=enum.IntFlag)
PixelFormat = _enum("PixelFormat", "V4L2_PIX_FMT_")
BufferType = _enum("BufferType", "V4L2_BUF_TYPE_")
Memory = _enum("Memory", "V4L2_MEMORY_")
ImageFormatFlag = _enum("ImageFormatFlag", "V4L2_FMT_FLAG_", klass=enum.IntFlag)
Field = _enum("Field", "V4L2_FIELD_")
FrameSizeType = _enum("FrameSizeType", "V4L2_FRMSIZE_TYPE_")
FrameIntervalType = _enum("FrameIntervalType", "V4L2_FRMIVAL_TYPE_")
IOC = _enum("IOC", "VIDIOC_", klass=enum.Enum)


Info = collections.namedtuple(
    "Info", "driver card bus_info version physical_capabilities capabilities crop_capabilities buffers formats frame_sizes")

ImageFormat = collections.namedtuple(
    "ImageFormat", "type description flags pixel_format")

Format = collections.namedtuple("Format", "width height pixel_format")

CropCapability = collections.namedtuple(
    "CropCapability", "type bounds defrect pixel_aspect")

Rect = collections.namedtuple("Rect", "left top width height")

Size = collections.namedtuple("Size", "width height")

FrameType = collections.namedtuple(
    "FrameType", "type pixel_format width height min_fps max_fps step_fps")


def frame_sizes(fd, pixel_formats):

    def get_frame_intervals(fmt, w, h):
        val = raw.v4l2_frmivalenum()
        val.pixel_format = fmt
        val.width = w
        val.height = h
        res = []
        for index in range(128):
            try:
                fcntl.ioctl(fd, IOC.ENUM_FRAMEINTERVALS.value, val)
            except OSError as error:
                if error.errno == errno.EINVAL:
                    break
                else:
                    raise
            val.index = index
            # values come in frame interval (fps = 1/interval)
            if val.type == FrameIntervalType.DISCRETE:
                min_fps = max_fps = step_fps = val.discrete.denominator / val.discrete.numerator
            else:
                min_fps = val.stepwise.min.denominator / val.stepwise.min.numerator
                max_fps = val.stepwise.max.denominator / val.stepwise.max.numerator
                step_fps = val.stepwise.step.denominator / val.stepwise.step.numerator
            res.append(FrameType(
                type=FrameIntervalType(val.type),
                pixel_format=fmt, width=w, height=h,
                min_fps=min_fps, max_fps=max_fps, step_fps=step_fps))
        return res

    size = raw.v4l2_frmsizeenum()
    sizes = []
    for pixel_format in pixel_formats:
        size.pixel_format = pixel_format
        fcntl.ioctl(fd, IOC.ENUM_FRAMESIZES.value, size)
        if size.type == FrameSizeType.DISCRETE:
            sizes += get_frame_intervals(pixel_format, size.discrete.width,
                                         size.discrete.height)
        else:
            step = size.stepwise
            for w in range(step.min_width, step.max_width, step.step_width):
                for h in range(step.min_height, step.max_height, step.step_height):
                    sizes += get_frame_intervals(pixel_format, w, h)
    return sizes


def read_info(fd):
    caps = raw.v4l2_capability()
    fcntl.ioctl(fd, IOC.QUERYCAP.value, caps)
    version_tuple = (
        (caps.version & 0xFF0000) >> 16,
        (caps.version & 0x00FF00) >> 8,
        (caps.version & 0x0000FF),
    )
    version_str = ".".join(map(str, version_tuple))
    device_capabilities=Capability(caps.device_caps)
    buffers = [
        typ for typ in BufferType
        if Capability[typ.name] in device_capabilities
    ]

    fmt = raw.v4l2_fmtdesc()
    img_fmt_stream_types = {
        BufferType.VIDEO_CAPTURE, BufferType.VIDEO_CAPTURE_MPLANE,
        BufferType.VIDEO_OUTPUT, BufferType.VIDEO_OUTPUT_MPLANE,
        BufferType.VIDEO_OVERLAY
    } & set(buffers)

    formats = []
    pixel_formats = set()
    for stream_type in img_fmt_stream_types:
        fmt.type = stream_type
        for index in range(128):
            fmt.index = index
            try:
                fcntl.ioctl(fd, IOC.ENUM_FMT.value, fmt)
            except OSError as error:
                if error.errno == errno.EINVAL:
                    break
                else:
                    raise
            if fmt.pixelformat == 0: # odroid v4l2 doesn't know Z16
                continue
            pixel_format = PixelFormat(fmt.pixelformat)
            formats.append(ImageFormat(
                type=stream_type,
                flags=ImageFormatFlag(fmt.flags),
                description=fmt.description,
                pixel_format=pixel_format))
            pixel_formats.add(pixel_format)

    crop = raw.v4l2_cropcap()
    crop_stream_types = {
        BufferType.VIDEO_CAPTURE, BufferType.VIDEO_OUTPUT, BufferType.VIDEO_OVERLAY
    } & set(buffers)
    crop_caps = []
    for stream_type in crop_stream_types:
        crop.type = stream_type
        fcntl.ioctl(fd, IOC.CROPCAP.value, crop)
        crop_caps.append(CropCapability(
            type=stream_type,
            bounds=Rect(crop.bounds.left, crop.bounds.top, crop.bounds.width, crop.bounds.height),
            defrect=Rect(crop.defrect.left, crop.defrect.top, crop.defrect.width, crop.defrect.height),
            pixel_aspect=crop.pixelaspect.numerator/crop.pixelaspect.denominator))

    return Info(
        driver=caps.driver.decode(),
        card=caps.card.decode(),
        bus_info=caps.bus_info.decode(),
        version=version_str,
        physical_capabilities=Capability(caps.capabilities),
        capabilities=device_capabilities,
        crop_capabilities=crop_caps,
        buffers=buffers,
        formats=formats,
        frame_sizes=frame_sizes(fd, pixel_formats),
    )


class Device:

    def __init__(self, filename):
        self._context_level = 0
        self._fd = os.open(filename, os.O_RDWR | os.O_NONBLOCK)
        self.info = read_info(self._fd)
        self.filename = filename
        if Capability.VIDEO_CAPTURE in self.info.capabilities:
            self.video_capture = VideoCapture(self)
        else:
            self.video_capture = None

    def __enter__(self):
        self._context_level += 1
        return self

    def __exit__(self, exc_type, exc_value, tb):
        self._context_level -= 1
        if not self._context_level:
            self.close()

    def __iter__(self):
        return iter(self.video_capture)

    def _ioctl(self, request, arg=0):
        return fcntl.ioctl(self, request, arg)

    @classmethod
    def from_id(self, did):
        return Device("/dev/video{}".format(did))

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def fileno(self):
        return self._fd

    @property
    def closed(self):
        return self._fd is None


class VideoCapture:

    def __init__(self, device, buffer_type=BufferType.VIDEO_CAPTURE):
        self.device = device
        self.buffer_type = buffer_type

    def __iter__(self):
        return iter(VideoStream(self))

    def _ioctl(self, request, arg=0):
        return self.device._ioctl(request.value, arg=arg)

    @property
    def formats(self):
        return [fmt for fmt in self.device.info.formats if fmt.type == self.buffer_type]

    @property
    def crop_capabilities(self):
        return [
            crop for crop in self.device.info.crop_capabilities
            if crop.type == self.buffer_type
        ]

    def set_format(self, width, height, pixel_format="MJPG"):
        f = raw.v4l2_format()
        if isinstance(pixel_format, str):
            pixel_format = raw.v4l2_fourcc(*pixel_format.upper())
        f.type = self.buffer_type
        f.fmt.pix.pixelformat = pixel_format
        f.fmt.pix.field = Field.ANY
        f.fmt.pix.width = width
        f.fmt.pix.height = height
        f.fmt.pix.bytesperline = 0
        return self._ioctl(IOC.S_FMT, f)

    def get_format(self):
        f = raw.v4l2_format()
        f.type = self.buffer_type
        self._ioctl(IOC.G_FMT, f)
        return Format(
            width=f.fmt.pix.width,
            height=f.fmt.pix.height,
            pixel_format=PixelFormat(f.fmt.pix.pixelformat)
        )

    def set_fps(self, fps):
        p = raw.v4l2_streamparm()
        p.type = self.buffer_type
        p.parm.capture.timeperframe.numerator = 1
        p.parm.capture.timeperframe.denominator = fps
        return self._ioctl(IOC.S_PARM, p)

    def get_fps(self):
        p = raw.v4l2_streamparm()
        p.type = self.buffer_type
        self._ioctl(IOC.G_PARM, p)
        return p.parm.capture.timeperframe.denominator

    def start(self):
        btype = raw.v4l2_buf_type(self.buffer_type)
        self._ioctl(IOC.STREAMON, btype)

    def stop(self):
        btype = raw.v4l2_buf_type(self.buffer_type)
        self._ioctl(IOC.STREAMOFF, btype)


class BaseBuffer:

    def __init__(self, device, index=0, buffer_type=BufferType.VIDEO_CAPTURE, queue=True):
        self._context_level = 0
        self.device = device
        self.index = index
        self.buffer_type = buffer_type
        self.queue = queue

    def _v4l2_buffer(self):
        buff = raw.v4l2_buffer()
        buff.index = self.index
        buff.type = self.buffer_type
        return buff

    def __enter__(self):
        self._context_level += 1
        return self

    def __exit__(self, exc_type, exc_value, tb):
        self._context_level -= 1
        if not self._context_level:
            self.close()

    def _ioctl(self, request, arg=0):
        return self.device._ioctl(request.value, arg=arg)

    def close(self):
        pass


class BufferMMAP(BaseBuffer):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        buff = self._v4l2_buffer()
        self._ioctl(IOC.QUERYBUF, buff)
        self.mmap = mmap.mmap(self.device.fileno(), buff.length, offset=buff.m.offset)
        self.length = buff.length
        if self.queue:
            self._ioctl(IOC.QBUF, buff)

    def _v4l2_buffer(self):
        buff = super()._v4l2_buffer()
        buff.memory = Memory.MMAP
        return buff

    def close(self):
        if self.mmap is not None:
            self.mmap.close()
            self.mmap = None

    def raw_read(self, buff):
        result = self.mmap[:buff.bytesused]
        if self.queue:
            self._ioctl(IOC.QBUF, buff)
        return result

    def read(self, buff):
        select.select((self.device,), (), ())
        return self.raw_read(buff)


class Buffers:

    def __init__(self, device, buffer_type=BufferType.VIDEO_CAPTURE,
                 buffer_size=1, buffer_queue=True, memory=Memory.MMAP):
        self._context_level = 0
        self.device = device
        self.buffer_size = buffer_size
        self.buffer_type = buffer_type
        self.buffer_queue = buffer_queue
        self.memory = memory
        self.buffers = self._create_buffers()

    def __enter__(self):
        self._context_level += 1
        return self

    def __exit__(self, exc_type, exc_value, tb):
        self._context_level -= 1
        if not self._context_level:
            self.close()

    def _ioctl(self, request, arg=0):
        return self.device._ioctl(request.value, arg=arg)

    def _create_buffers(self):
        if self.memory != Memory.MMAP:
            raise TypeError(f"Unsupported buffer type {self.memory.name!r}")
        r = raw.v4l2_requestbuffers()
        r.count = self.buffer_size
        r.type = self.buffer_type
        r.memory = self.memory
        self._ioctl(IOC.REQBUFS, r)
        if not r.count:
            raise IOError("Not enough buffer memory")
        return [
            BufferMMAP(self.device, index, self.buffer_type, self.buffer_queue)
            for index in range(r.count)
        ]

    def close(self):
        if self.buffers:
            for buff in self.buffers:
                buff.close()
            self.buffers = None

    def raw_read(self):
        buff = self.buffers[0]._v4l2_buffer()
        self._ioctl(IOC.DQBUF, buff)
        return self.buffers[buff.index].raw_read(buff)

    def read(self):
        select.select((self.device,), (), ())
        return self.raw_read()


class VideoStream:

    def __init__(self, video_capture, buffer_size=1, buffer_queue=True,
                 memory=Memory.MMAP):
        self._context_level = 0
        self.video_capture = video_capture
        self.buffers = Buffers(
            video_capture.device, video_capture.buffer_type,
            buffer_size, buffer_queue, memory)

    def __enter__(self):
        self._context_level += 1
        return self

    def __exit__(self, exc_type, exc_value, tb):
        self._context_level -= 1
        if not self._context_level:
            self.close()

    def __iter__(self):
        return Stream(self)

    async def __aiter__(self):
        async for frame in AsyncStream(self):
            yield frame

    def close(self):
        self.buffers.close()

    def raw_read(self):
        return self.buffers.raw_read()

    def read(self):
        return self.buffers.read()


def Stream(stream):
    stream.video_capture.start()
    try:
        while True:
            yield stream.read()
    finally:
        stream.video_capture.stop()


async def AsyncStream(stream):
    import asyncio
    cap = stream.video_capture
    fd = cap.device.fileno()
    loop = asyncio.get_event_loop()
    event = asyncio.Event()
    loop.add_reader(fd, event.set)
    try:
        cap.start()
        while True:
            await event.wait()
            event.clear()
            yield stream.raw_read()
    finally:
        cap.stop()
        loop.remove_reader(fd)

def iter_devices(path="/dev"):
    path = pathlib.Path(path)
    files = path.glob("video*")
    return (Device(name) for name in files)


def iter_video_capture_devices(path="/dev"):
    f = lambda d: Capability.VIDEO_CAPTURE in d.info.capabilities
    return filter(f, iter_devices(path))
