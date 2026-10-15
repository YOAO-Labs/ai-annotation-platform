"""Strict task and polygon contracts, without native framework dependencies."""

from fractions import Fraction
import hashlib
import math
import struct

DETECTION = "image-detection"
SEGMENTATION = "image-instance-segmentation"
SEGMENTATION_CONTRACT = {
    "schema": 2,
    "task": SEGMENTATION,
    "geometry": "normalized-simple-polygon",
    "bridge": "ultralytics-simple-polygon-1",
}
MAX_VERTICES = 512
MAX_INSTANCES = 300
MAX_IMAGE_VERTICES = 8192


class DatasetContractError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def data_contract(record):
    schema = record.get("schema")
    if type(schema) is int and schema == 1:
        if (
            record.get("task", DETECTION) != DETECTION
            or "geometry" in record
            or "bridge" in record
            or record.get("format", "ultralytics-detection") != "ultralytics-detection"
        ):
            raise DatasetContractError("unknown_data_contract", "未知的检测数据身份")
        return {"schema": 1, "task": DETECTION}
    if (
        type(schema) is int
        and schema == 2
        and all(
            record.get(key) == value for key, value in SEGMENTATION_CONTRACT.items()
        )
        and record.get("format", "ultralytics-segmentation")
        == "ultralytics-segmentation"
    ):
        return dict(SEGMENTATION_CONTRACT)
    raise DatasetContractError(
        "unknown_data_contract", "未知的训练任务、几何或桥接身份"
    )


def config_task(config):
    identity = (config.get("configuration") or {}).get("identity", {})
    return identity.get("task", DETECTION)


def _cross(a, b, c):
    left = (b[0] - a[0]) * (c[1] - a[1])
    right = (b[1] - a[1]) * (c[0] - a[0])
    value = left - right
    if abs(value) > 1e-14:
        return value
    # Exact fallback distinguishes tiny valid edges from collinear contacts.
    ax, ay, bx, by, cx, cy = map(Fraction, (*a, *b, *c))
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _on_segment(a, b, c):
    return (
        min(a[0], b[0]) <= c[0] <= max(a[0], b[0])
        and min(a[1], b[1]) <= c[1] <= max(a[1], b[1])
        and _cross(a, b, c) == 0
    )


def polygon_area(points):
    terms = [
        a[0] * b[1] - b[0] * a[1]
        for a, b in zip(points, points[1:] + points[:1], strict=True)
    ]
    value = math.fsum(terms)
    if abs(value) <= 1e-12:
        value = sum(
            Fraction(a[0]) * Fraction(b[1]) - Fraction(b[0]) * Fraction(a[1])
            for a, b in zip(points, points[1:] + points[:1], strict=True)
        )
    return float(abs(value) / 2)


def _validate_ring(points):
    if len(set(points)) != len(points) or polygon_area(points) == 0:
        raise DatasetContractError("invalid_polygon", "多边形顶点重复或面积为零")
    count = len(points)
    for i, b in enumerate(points):
        a, c = points[i - 1], points[(i + 1) % count]
        if _cross(a, b, c) == 0 and (_on_segment(a, b, c) or _on_segment(b, c, a)):
            raise DatasetContractError("invalid_polygon", "多边形边不能折返或重叠")
    for i, a in enumerate(points):
        b = points[(i + 1) % count]
        for j in range(i + 2, count):
            if i == 0 and j == count - 1:
                continue
            c, d = points[j], points[(j + 1) % count]
            if (
                max(a[0], b[0]) < min(c[0], d[0])
                or max(c[0], d[0]) < min(a[0], b[0])
                or max(a[1], b[1]) < min(c[1], d[1])
                or max(c[1], d[1]) < min(a[1], b[1])
            ):
                continue
            ab_c, ab_d = _cross(a, b, c), _cross(a, b, d)
            cd_a, cd_b = _cross(c, d, a), _cross(c, d, b)
            if (
                ab_c * ab_d < 0
                and cd_a * cd_b < 0
                or _on_segment(a, b, c)
                or _on_segment(a, b, d)
                or _on_segment(c, d, a)
                or _on_segment(c, d, b)
            ):
                raise DatasetContractError("invalid_polygon", "多边形不能自交或自接触")


def _canonical(points):
    start = points.index(min(points))
    forward = tuple(points[start:] + points[:start])
    return min(forward, (forward[0], *reversed(forward[1:])))


def polygon_points(raw):
    if not isinstance(raw, (list, tuple)) or not 3 <= len(raw) <= MAX_VERTICES + 1:
        raise DatasetContractError("polygon_limit", "单个多边形需要 3 至 512 个顶点")
    points = []
    for point in raw:
        if (
            not isinstance(point, (list, tuple))
            or len(point) != 2
            or any(
                type(v) not in {int, float} or not 0 <= v <= 1 or not math.isfinite(v)
                for v in point
            )
        ):
            raise DatasetContractError(
                "invalid_polygon", "多边形坐标必须是 [0,1] 内的有限数值"
            )
        points.append(tuple(float(v) if v else 0.0 for v in point))
    if points[-1] == points[0]:
        points.pop()
    if not 3 <= len(points) <= MAX_VERTICES:
        raise DatasetContractError("polygon_limit", "单个多边形需要 3 至 512 个顶点")
    _validate_ring(points)
    native = [
        tuple(struct.unpack("f", struct.pack("f", v))[0] for v in p) for p in points
    ]
    try:
        _validate_ring(native)
    except DatasetContractError as exc:
        raise DatasetContractError(
            "native_polygon_collapse", "多边形转为原生 float32 后退化"
        ) from exc
    return _canonical(points), _canonical(native)


def segmentation_labels(annotations, classes):
    """Return canonical labels and a stable line-to-source instance receipt."""
    if len(annotations) > MAX_INSTANCES:
        raise DatasetContractError("instance_limit", "每张图片最多包含 300 个训练实例")
    rows, signatures, ids, vertices = [], set(), set(), 0
    for annotation in annotations:
        geometry = annotation.get("geometry", {})
        if (
            annotation.get("tool_unit_id") != "region"
            or geometry.get("type") != "polygon"
            or geometry.get("holes", []) != []
            or set(geometry) - {"type", "points", "holes"}
            or annotation.get("class_name") not in classes
        ):
            raise DatasetContractError(
                "unsupported_annotation",
                "分割训练仅接受冻结类别表中的无洞单多边形 region 标注",
            )
        identifier, version = annotation.get("id"), annotation.get("version")
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier in ids
            or type(version) is not int
            or version < 1
        ):
            raise DatasetContractError(
                "invalid_instance_identity", "分割实例必须保留唯一标注标识及版本"
            )
        ids.add(identifier)
        points, native = polygon_points(geometry.get("points"))
        vertices += len(points)
        if vertices > MAX_IMAGE_VERTICES:
            raise DatasetContractError(
                "polygon_limit", "每张图片最多包含 8192 个训练顶点"
            )
        index = classes.index(annotation["class_name"])
        signature = (index, native)
        if signature in signatures:
            raise DatasetContractError(
                "duplicate_instance",
                "同类别的多边形在原生 float32 中重复，无法保留实例身份",
            )
        signatures.add(signature)
        line = str(index) + " " + " ".join(format(v, ".17g") for p in points for v in p)
        rows.append((line, identifier, version, index))
    rows.sort()
    text = "\n".join(row[0] for row in rows) + ("\n" if rows else "")
    instances = [
        {
            "line": line,
            "annotation_id": identifier,
            "annotation_version": version,
            "class_index": index,
            "line_sha256": hashlib.sha256(value.encode()).hexdigest(),
        }
        for line, (value, identifier, version, index) in enumerate(rows, 1)
    ]
    return text, instances
