import struct, gzip, zlib, io

TAG_END, TAG_BYTE, TAG_SHORT, TAG_INT, TAG_LONG, TAG_FLOAT, TAG_DOUBLE, TAG_BYTE_ARRAY, TAG_STRING, TAG_LIST, TAG_COMPOUND, TAG_INT_ARRAY, TAG_LONG_ARRAY = range(13)


class Tag:
    __slots__ = ("type", "value", "elem")

    def __init__(self, type, value, elem=None):
        self.type = type
        self.value = value
        self.elem = elem  # list element type

    def __getitem__(self, k):
        return self.value[k]

    def get(self, k, d=None):
        v = self.value.get(k)
        return v if v is not None else d

    def __contains__(self, k):
        return k in self.value

    def __repr__(self):
        return f"Tag({self.type},{self.value!r})"


class _R:
    def __init__(self, b):
        self.b = b
        self.p = 0

    def take(self, n):
        v = self.b[self.p:self.p + n]
        self.p += n
        return v

    def unpack(self, f):
        s = struct.calcsize(f)
        v = struct.unpack_from(f, self.b, self.p)
        self.p += s
        return v[0]

    def string(self):
        n = self.unpack(">H")
        return self.take(n).decode("utf-8", "replace")

    def payload(self, t):
        if t == TAG_BYTE: return self.unpack(">b")
        if t == TAG_SHORT: return self.unpack(">h")
        if t == TAG_INT: return self.unpack(">i")
        if t == TAG_LONG: return self.unpack(">q")
        if t == TAG_FLOAT: return self.unpack(">f")
        if t == TAG_DOUBLE: return self.unpack(">d")
        if t == TAG_BYTE_ARRAY: return self.take(self.unpack(">i"))
        if t == TAG_STRING: return self.string()
        if t == TAG_LIST:
            et = self.unpack(">b")
            n = self.unpack(">i")
            return [self._tag(et) for _ in range(n)], et
        if t == TAG_COMPOUND:
            d = {}
            while True:
                ct = self.unpack(">b")
                if ct == TAG_END:
                    return d
                name = self.string()
                d[name] = self._tag(ct)
        if t == TAG_INT_ARRAY:
            n = self.unpack(">i")
            v = struct.unpack_from(">%di" % n, self.b, self.p); self.p += 4 * n
            return list(v)
        if t == TAG_LONG_ARRAY:
            n = self.unpack(">i")
            v = struct.unpack_from(">%dq" % n, self.b, self.p); self.p += 8 * n
            return list(v)
        raise ValueError("bad tag %d" % t)

    def _tag(self, t):
        if t == TAG_LIST:
            v, et = self.payload(t)
            return Tag(t, v, et)
        return Tag(t, self.payload(t))


def loads(b):
    r = _R(b)
    t = r.unpack(">b")
    name = r.string()
    return name, r._tag(t)


def _w_payload(out, tag):
    t, v = tag.type, tag.value
    if t == TAG_BYTE: out.write(struct.pack(">b", v))
    elif t == TAG_SHORT: out.write(struct.pack(">h", v))
    elif t == TAG_INT: out.write(struct.pack(">i", v))
    elif t == TAG_LONG: out.write(struct.pack(">q", v))
    elif t == TAG_FLOAT: out.write(struct.pack(">f", v))
    elif t == TAG_DOUBLE: out.write(struct.pack(">d", v))
    elif t == TAG_BYTE_ARRAY: out.write(struct.pack(">i", len(v))); out.write(bytes(v))
    elif t == TAG_STRING:
        b = v.encode("utf-8"); out.write(struct.pack(">H", len(b))); out.write(b)
    elif t == TAG_LIST:
        out.write(struct.pack(">bi", tag.elem if v else (tag.elem or TAG_END), len(v)))
        for e in v: _w_payload(out, e)
    elif t == TAG_COMPOUND:
        for k, e in v.items():
            out.write(struct.pack(">b", e.type))
            kb = k.encode("utf-8"); out.write(struct.pack(">H", len(kb))); out.write(kb)
            _w_payload(out, e)
        out.write(b"\x00")
    elif t == TAG_INT_ARRAY: out.write(struct.pack(">i%di" % len(v), len(v), *v))
    elif t == TAG_LONG_ARRAY: out.write(struct.pack(">i%dq" % len(v), len(v), *v))
    else: raise ValueError(t)


def dumps(name, tag):
    out = io.BytesIO()
    out.write(struct.pack(">b", tag.type))
    nb = name.encode("utf-8"); out.write(struct.pack(">H", len(nb))); out.write(nb)
    _w_payload(out, tag)
    return out.getvalue()


def read_gz(path_or_bytes):
    b = path_or_bytes if isinstance(path_or_bytes, (bytes, bytearray)) else open(path_or_bytes, "rb").read()
    return loads(gzip.decompress(b))


def write_gz(path, name, tag):
    with open(path, "wb") as f:
        f.write(gzip.compress(dumps(name, tag), mtime=0))
