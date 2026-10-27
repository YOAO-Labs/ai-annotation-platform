"""Strict task and polygon contracts, without native framework dependencies."""

from fractions import Fraction
import hashlib
import math
import json
import struct

from aap_task_contract import normalize_task, resolve_task

DETECTION = "detect"
SEGMENTATION = "segment"
LEGACY_DETECTION = "image-detection"
LEGACY_SEGMENTATION_CONTRACT = {
    "schema": 2,
    "task": "image-instance-segmentation",
    "geometry": "normalized-simple-polygon",
    "bridge": "ultralytics-simple-polygon-1",
}
DETECTION_CONTRACT = {
    "schema": 3,
    "task": DETECTION,
    "media_type": "image",
    "format": "ultralytics-yolo",
    "geometry": "normalized-bbox",
    "bridge": "ultralytics-bbox-1",
}
SEGMENTATION_CONTRACT = {
    "schema": 3,
    "task": SEGMENTATION,
    "media_type": "image",
    "format": "ultralytics-yolo",
    "geometry": "normalized-simple-polygon",
    "bridge": "ultralytics-simple-polygon-1",
}
INSTANCE_RLE_CONTRACT = {
    "schema": 4,
    "task": SEGMENTATION,
    "media_type": "image",
    "format": "aap-instance-rle",
    "geometry": "instance-mask",
    "bridge": "ultralytics-instance-rle-1",
}
INSTANCE_RLE_LIMITS = {
    "max_image_pixels": 16_000_000,
    "max_instances": 300,
    "max_sample_rle_bytes": 16 * 1024 * 1024,
    "max_sample_label_bytes": 32 * 1024 * 1024,
    "max_dataset_bytes": 20 * 1024**3,
    "max_package_bytes": 20 * 1024**3,
    "max_original_mask_elements": 128_000_000,
    "max_batch_mask_elements": 64_000_000,
    "workers": 0,
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
            record.get("task", LEGACY_DETECTION) != LEGACY_DETECTION
            or "geometry" in record
            or "bridge" in record
            or record.get("format", "ultralytics-detection") != "ultralytics-detection"
        ):
            raise DatasetContractError("unknown_data_contract", "未知的检测数据身份")
        return {"schema": 1, "task": LEGACY_DETECTION}
    if (
        type(schema) is int
        and schema == 2
        and all(
            record.get(key) == value
            for key, value in LEGACY_SEGMENTATION_CONTRACT.items()
        )
        and record.get("format", "ultralytics-segmentation")
        == "ultralytics-segmentation"
    ):
        return dict(LEGACY_SEGMENTATION_CONTRACT)
    if type(schema) is int and schema == 3:
        expected = {
            DETECTION: DETECTION_CONTRACT,
            SEGMENTATION: SEGMENTATION_CONTRACT,
        }.get(record.get("task"))
        if expected and all(
            record.get(key) == value for key, value in expected.items()
        ):
            return dict(expected)
    if (
        type(schema) is int
        and schema == 4
        and all(
            record.get(key) == value for key, value in INSTANCE_RLE_CONTRACT.items()
        )
    ):
        return dict(INSTANCE_RLE_CONTRACT)
    raise DatasetContractError(
        "unknown_data_contract", "未知的训练任务、几何或桥接身份"
    )


def config_task(config):
    identity = (config.get("configuration") or {}).get("identity", {})
    return resolve_task(config.get("task"), identity.get("task")) or DETECTION


def data_task(record):
    return normalize_task(data_contract(record)["task"])


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


def is_instance_rle(contract):
    return contract.get("bridge") == INSTANCE_RLE_CONTRACT["bridge"]


def instance_rle_budget(width, height, instances, encoded_bytes=0):
    """Reject before decoding, including empty images and all-instance work."""
    if any(type(v) is not int or v <= 0 for v in (width, height)):
        raise DatasetContractError("mask_dimensions", "掩码训练需要确定的原始图片尺寸")
    if width * height > INSTANCE_RLE_LIMITS["max_image_pixels"]:
        raise DatasetContractError("mask_pixel_limit", "单图最多包含 1600 万像素")
    if type(instances) is not int or not 0 <= instances <= MAX_INSTANCES:
        raise DatasetContractError("instance_limit", "每张图片最多包含 300 个训练实例")
    if width * height * instances > INSTANCE_RLE_LIMITS["max_original_mask_elements"]:
        raise DatasetContractError(
            "mask_decode_limit", "单样本原尺寸掩码累计解码量超过 1.28 亿元素"
        )
    if (
        type(encoded_bytes) is not int
        or not 0 <= encoded_bytes <= INSTANCE_RLE_LIMITS["max_sample_rle_bytes"]
    ):
        raise DatasetContractError("mask_encoded_limit", "单样本 RLE 总量超过 16 MiB")


def instance_rle_bytes(rle):
    """Canonical uncompressed COCO RLE; validates without dense expansion."""
    if not isinstance(rle, dict) or rle.get("encoding") != "coco_rle":
        raise DatasetContractError("invalid_rle", "仅接受 canonical COCO RLE")
    size, counts = rle.get("size"), rle.get("counts")
    if not isinstance(size, list) or len(size) != 2:
        raise DatasetContractError("invalid_rle", "RLE 尺寸必须为 [height,width]")
    instance_rle_budget(size[1], size[0], 1)
    if (
        not isinstance(counts, list)
        or not counts
        or len(counts) > 1_000_000
        or any(type(v) is not int or v < 0 for v in counts)
        or sum(counts) != size[0] * size[1]
    ):
        raise DatasetContractError("invalid_rle", "RLE run 必须有界且覆盖完整图像")
    data = json.dumps(
        {"encoding": "coco_rle", "size": size, "counts": counts},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    if len(data) > 4 * 1024 * 1024:
        raise DatasetContractError("invalid_rle", "单实例 canonical RLE 超过 4 MiB")
    return data


def instance_rle_evidence(rle):
    """Pixel identity ignores run segmentation; no original-size dense buffer."""
    encoded = instance_rle_bytes(rle)
    height, width = rle["size"]
    hasher = hashlib.sha256(struct.pack(">II", height, width))
    offset, area = 0, 0
    xmin, ymin, xmax, ymax = width, height, -1, -1
    for index, count in enumerate(rle["counts"]):
        value = index % 2
        chunk = bytes([value]) * min(count, 65536)
        remaining = count
        while remaining:
            take = min(remaining, len(chunk))
            hasher.update(chunk[:take])
            remaining -= take
        if value and count:
            first_x, first_y = divmod(offset, height)
            last_x, last_y = divmod(offset + count - 1, height)
            xmin, xmax = min(xmin, first_x), max(xmax, last_x)
            ymin = min(ymin, first_y if first_x == last_x else 0)
            ymax = max(ymax, last_y if first_x == last_x else height - 1)
            area += count
        offset += count
    if not area:
        raise DatasetContractError("empty_instance", "训练实例掩码不能为空")
    return {
        "size": [height, width],
        "area_pixels": area,
        "bbox_xyxy": [xmin, ymin, xmax + 1, ymax + 1],
        "pixel_sha256": hasher.hexdigest(),
        "rle_sha256": hashlib.sha256(encoded).hexdigest(),
        "rle_bytes": len(encoded),
    }


def instance_mask_annotations(annotations, classes, width, height):
    """Bounded snapshot metadata validation, without reading mask objects."""
    instance_rle_budget(width, height, len(annotations))
    seen, encoded = set(), 0
    for annotation in annotations:
        identifier, version = annotation.get("id"), annotation.get("version")
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier in seen
            or type(version) is not int
            or version < 1
        ):
            raise DatasetContractError(
                "invalid_instance_identity", "实例必须保留唯一标注标识及版本"
            )
        seen.add(identifier)
        geometry = annotation.get("geometry", {})
        if (
            annotation.get("tool_unit_id") != "region"
            or annotation.get("class_name") not in classes
        ):
            raise DatasetContractError(
                "unsupported_annotation", "实例必须属于冻结 region 类别表"
            )
        if geometry.get("type") == "polygon":
            # The first revision rasterizes only the already qualified simple
            # polygon surface; holes/multipart are accepted as native raster.
            segmentation_labels([annotation], classes)
        elif geometry.get("type") == "raster_mask":
            ref = geometry.get("mask", {})
            if (
                not isinstance(ref, dict)
                or ref.get("size") != [height, width]
                or type(ref.get("bytes")) is not int
                or ref["bytes"] <= 0
                or ref["bytes"] > 4 * 1024 * 1024
                or ref.get("encoding") not in {"coco_rle_ref", "coco_rle_gzip"}
                or not isinstance(ref.get("sha256"), str)
                or len(ref["sha256"]) != 64
                or not isinstance(ref.get("object_key"), str)
                or not ref["object_key"].startswith("raster-masks/sha256/")
            ):
                raise DatasetContractError(
                    "invalid_mask_reference", "掩码引用缺少有界尺寸、字节数或内容身份"
                )
            encoded += ref["bytes"]
        else:
            raise DatasetContractError(
                "unsupported_annotation",
                "原生掩码桥接仅接受 raster mask 或合格简单多边形",
            )
    instance_rle_budget(width, height, len(annotations), encoded)
