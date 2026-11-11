import os
from pathlib import Path


_EASYOCR_READER = None
_EASYOCR_READER_DIR = None


def _default_easyocr_model_dir():
    return str(Path(__file__).resolve().parents[2] / ".easyocr" / "model")


def _make_easyocr_reader(easyocr, model_storage_directory=None):
    model_dir = (
        model_storage_directory
        or os.environ.get("EASYOCR_MODEL_DIR")
        or _default_easyocr_model_dir()
    )
    return easyocr.Reader(
        ["ru", "en"],
        gpu=False,
        model_storage_directory=model_dir,
        user_network_directory=str(Path(model_dir) / "user_network"),
    )


def _get_easyocr_reader(easyocr):
    global _EASYOCR_READER, _EASYOCR_READER_DIR
    model_dir = os.environ.get("EASYOCR_MODEL_DIR") or _default_easyocr_model_dir()
    if _EASYOCR_READER is None or _EASYOCR_READER_DIR != model_dir:
        _EASYOCR_READER = _make_easyocr_reader(easyocr, model_storage_directory=model_dir)
        _EASYOCR_READER_DIR = model_dir
    return _EASYOCR_READER


import re as _re

_ORIENT_WORD_RE = _re.compile(r"[А-Яа-яЁё]{3,}")
_ORIENT_NUM_RE = _re.compile(r"\d{2,}")


def detect_orientation(image, candidates=(0, 90, 180, 270), thumb_max=1100):
    """Подобрать поворот (0/90/180/270), при котором OCR читает текст, а не шум.

    Фото документа могут быть сняты в любой ориентации; жёсткий поворот ломает
    распознавание. Гоняем OCR по уменьшенной копии на всех углах и берём тот,
    где больше осмысленных слов/чисел.
    """
    import cv2
    import easyocr
    import numpy as np

    reader = _get_easyocr_reader(easyocr)
    height, width = image.shape[:2]
    scale = min(thumb_max / max(height, width), 1.0)
    thumb = cv2.resize(image, (int(width * scale), int(height * scale))) if scale < 1 else image

    best_angle, best_score = candidates[0], -1
    for angle in candidates:
        rotated = _rotate_image(cv2, thumb, angle)
        rgb = cv2.cvtColor(rotated, cv2.COLOR_BGR2RGB)
        texts = reader.readtext(np.array(rgb), paragraph=False, detail=0)
        score = sum(
            len(_ORIENT_WORD_RE.findall(t)) + len(_ORIENT_NUM_RE.findall(t))
            for t in texts
        )
        if score > best_score:
            best_angle, best_score = angle, score
    return best_angle


def detect_cells(image, angle=90, crop_top_ratio=0.40):
    import cv2
    import easyocr
    import numpy as np

    rotated = _rotate_image(cv2, image, angle)
    if crop_top_ratio:
        crop_top = int(rotated.shape[0] * crop_top_ratio)
        rotated = rotated[crop_top:, :]
    table, quad = _extract_largest_table_region(cv2, np, rotated)
    grid = _detect_grid_lines(cv2, np, table)
    curves = _trace_grid_curves(cv2, np, table, grid)
    cells = _make_cells(grid["x_lines"], grid["y_lines"])

    reader = _get_easyocr_reader(easyocr)
    rgb = cv2.cvtColor(table, cv2.COLOR_BGR2RGB)
    ocr_result = reader.readtext(np.array(rgb), paragraph=False, detail=1)
    cells = _assign_ocr_to_cells(cells, ocr_result)
    return cells, {
        "angle": angle,
        "quad": quad,
        "x_lines": len(grid["x_lines"]),
        "y_lines": len(grid["y_lines"]),
        "grid_mode": "curved",
        "curve_vertical_lines": len(curves["vertical"]),
        "curve_horizontal_lines": len(curves["horizontal"]),
    }


def _rotate_image(cv2, image, angle):
    if angle == 0:
        return image.copy()
    if angle == 90:
        return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if angle == 180:
        return cv2.rotate(image, cv2.ROTATE_180)
    if angle == 270:
        return cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    raise ValueError(f"unsupported angle: {angle}")


def _group_positions(positions, tolerance=8):
    if not positions:
        return []
    grouped = []
    current = [positions[0]]
    for value in positions[1:]:
        if value - current[-1] <= tolerance:
            current.append(value)
        else:
            grouped.append(int(round(sum(current) / len(current))))
            current = [value]
    grouped.append(int(round(sum(current) / len(current))))
    return grouped


def _projection_peaks(projection, threshold, window=4):
    peaks = []
    for idx, value in enumerate(projection):
        if value < threshold:
            continue
        start = max(idx - window, 0)
        end = min(idx + window + 1, len(projection))
        if value >= max(projection[start:end]):
            peaks.append(idx)
    return peaks


def _hough_line_positions(cv2, np, binary):
    edges = cv2.Canny(binary, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=35, minLineLength=35, maxLineGap=20)
    x_positions = []
    y_positions = []
    if lines is None:
        return x_positions, y_positions
    for line in lines[:, 0]:
        x1, y1, x2, y2 = [int(value) for value in line]
        dx = abs(x2 - x1)
        dy = abs(y2 - y1)
        if dx < 1 and dy < 1:
            continue
        if dx <= dy * 0.45 and dy >= 35:
            x_positions.append((x1 + x2) // 2)
        elif dy <= dx * 0.35 and dx >= 35:
            y_positions.append((y1 + y2) // 2)
    return x_positions, y_positions


def _detect_grid_lines(cv2, np, image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    binary = cv2.adaptiveThreshold(
        blurred, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 12)
    height, width = binary.shape
    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(width // 35, 25), 1))
    vertical_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(height // 35, 25)))
    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, horizontal_kernel, iterations=1)
    vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN, vertical_kernel, iterations=1)

    y_projection = np.count_nonzero(horizontal, axis=1)
    x_projection = np.count_nonzero(vertical, axis=0)
    y_positions = _projection_peaks(y_projection, max(width * 0.05, 30))
    x_positions = _projection_peaks(x_projection, max(height * 0.08, 25))

    plain_y_projection = np.count_nonzero(binary, axis=1)
    plain_x_projection = np.count_nonzero(binary, axis=0)
    plain_y_threshold = max(width * 0.08, float(np.median(plain_y_projection) + np.std(plain_y_projection) * 2.5))
    plain_x_threshold = max(height * 0.20, float(np.median(plain_x_projection) + np.std(plain_x_projection) * 2.5))
    y_positions.extend(_projection_peaks(plain_y_projection, plain_y_threshold))
    x_positions.extend(_projection_peaks(plain_x_projection, plain_x_threshold))
    hough_x, hough_y = _hough_line_positions(cv2, np, binary)
    x_positions.extend(hough_x)
    return {
        "x_lines": _group_positions(sorted(x_positions)),
        "y_lines": _group_positions(sorted(y_positions)),
    }


def _grid_score(grid, image_shape):
    x_lines = grid["x_lines"]
    y_lines = grid["y_lines"]
    if len(x_lines) < 3 or len(y_lines) < 3:
        return 0
    height, width = image_shape[:2]
    x_span = max(x_lines) - min(x_lines)
    y_span = max(y_lines) - min(y_lines)
    coverage = (x_span / max(width, 1)) * (y_span / max(height, 1))
    return len(x_lines) * len(y_lines) * coverage


def _order_quad_points(np, points):
    pts = np.array(points, dtype="float32")
    sums = pts.sum(axis=1)
    diffs = np.diff(pts, axis=1).reshape(-1)
    return np.array([
        pts[np.argmin(sums)],
        pts[np.argmin(diffs)],
        pts[np.argmax(sums)],
        pts[np.argmax(diffs)],
    ], dtype="float32")


def _warp_quad(cv2, np, image, quad):
    tl, tr, br, bl = _order_quad_points(np, quad)
    width_a = np.linalg.norm(br - bl)
    width_b = np.linalg.norm(tr - tl)
    height_a = np.linalg.norm(tr - br)
    height_b = np.linalg.norm(tl - bl)
    max_width = max(int(width_a), int(width_b), 1)
    max_height = max(int(height_a), int(height_b), 1)
    destination = np.array(
        [[0, 0], [max_width - 1, 0], [max_width - 1, max_height - 1], [0, max_height - 1]],
        dtype="float32",
    )
    matrix = cv2.getPerspectiveTransform(np.array([tl, tr, br, bl], dtype="float32"), destination)
    return cv2.warpPerspective(image, matrix, (max_width, max_height))


def _expand_quad(np, quad, image_shape, margin_x_ratio=0.012, margin_y_ratio=0.045):
    height, width = image_shape[:2]
    ordered = _order_quad_points(np, quad)
    center = ordered.mean(axis=0)
    expanded = []
    for point in ordered:
        vec = point - center
        extra = np.array([
            width * margin_x_ratio if vec[0] >= 0 else -width * margin_x_ratio,
            height * margin_y_ratio if vec[1] >= 0 else -height * margin_y_ratio,
        ], dtype=np.float32)
        expanded.append([
            float(np.clip(point[0] + extra[0], 0, width - 1)),
            float(np.clip(point[1] + extra[1], 0, height - 1)),
        ])
    return np.array(expanded, dtype=np.float32)


def _intersect_lines(line_a, line_b):
    a1, b1, c1 = line_a
    a2, b2, c2 = line_b
    det = a1 * b2 - a2 * b1
    if abs(det) < 1e-6:
        return None
    x = (b1 * c2 - b2 * c1) / det
    y = (c1 * a2 - c2 * a1) / det
    return [x, y]


def _fit_border_line(cv2, np, points):
    if len(points) < 2:
        return None
    pts = np.array(points, dtype=np.float32)
    vx, vy, x0, y0 = cv2.fitLine(pts, cv2.DIST_L2, 0, 0.01, 0.01).reshape(-1)
    return float(vy), float(-vx), float(vx * y0 - vy * x0)


def _quad_from_border_lines(cv2, np, image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    binary = cv2.adaptiveThreshold(
        blurred, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 12)
    height, width = binary.shape
    edges = cv2.Canny(binary, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(
        edges, 1, np.pi / 180, threshold=80,
        minLineLength=max(80, width // 5), maxLineGap=24,
    )
    if lines is None:
        return None

    horizontal_segments = []
    vertical_segments = []
    for x1, y1, x2, y2 in lines[:, 0]:
        dx = abs(int(x2) - int(x1))
        dy = abs(int(y2) - int(y1))
        if dx > width * 0.22 and dy <= dx * 0.12:
            horizontal_segments.append((x1, y1, x2, y2))
        elif dy > height * 0.20 and dx <= dy * 0.22:
            vertical_segments.append((x1, y1, x2, y2))
    if len(horizontal_segments) < 2 or len(vertical_segments) < 2:
        return None

    top_y = min(min(y1, y2) for x1, y1, x2, y2 in horizontal_segments)
    bottom_y = max(max(y1, y2) for x1, y1, x2, y2 in horizontal_segments)
    left_x = min(min(x1, x2) for x1, y1, x2, y2 in vertical_segments)
    right_x = max(max(x1, x2) for x1, y1, x2, y2 in vertical_segments)
    y_band = max(14, int(height * 0.08))
    x_band = max(14, int(width * 0.04))

    top_points = []
    bottom_points = []
    for x1, y1, x2, y2 in horizontal_segments:
        if min(abs(y1 - top_y), abs(y2 - top_y)) <= y_band:
            top_points.extend([(x1, y1), (x2, y2)])
        if min(abs(y1 - bottom_y), abs(y2 - bottom_y)) <= y_band:
            bottom_points.extend([(x1, y1), (x2, y2)])

    left_points = []
    right_points = []
    for x1, y1, x2, y2 in vertical_segments:
        if min(abs(x1 - left_x), abs(x2 - left_x)) <= x_band:
            left_points.extend([(x1, y1), (x2, y2)])
        if min(abs(x1 - right_x), abs(x2 - right_x)) <= x_band:
            right_points.extend([(x1, y1), (x2, y2)])

    top = _fit_border_line(cv2, np, top_points)
    bottom = _fit_border_line(cv2, np, bottom_points)
    left = _fit_border_line(cv2, np, left_points)
    right = _fit_border_line(cv2, np, right_points)
    if not all((top, bottom, left, right)):
        return None

    quad = [
        _intersect_lines(top, left),
        _intersect_lines(top, right),
        _intersect_lines(bottom, right),
        _intersect_lines(bottom, left),
    ]
    if any(point is None for point in quad):
        return None
    return _expand_quad(np, np.array(quad, dtype=np.float32), image.shape, margin_x_ratio=0.006, margin_y_ratio=0.025)


def _contour_quad(cv2, contour):
    hull = cv2.convexHull(contour)
    perimeter = cv2.arcLength(hull, True)
    for ratio in (0.015, 0.02, 0.03, 0.04, 0.06, 0.08):
        approx = cv2.approxPolyDP(hull, ratio * perimeter, True)
        if len(approx) == 4:
            return approx.reshape(4, 2)
    rect = cv2.minAreaRect(hull.reshape(-1, 2))
    return cv2.boxPoints(rect)


def _extract_largest_table_region(cv2, np, image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    binary = cv2.adaptiveThreshold(
        blurred, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 12)
    height, width = binary.shape
    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(width // 45, 25), 1))
    vertical_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(height // 45, 18)))
    line_mask = cv2.morphologyEx(binary, cv2.MORPH_OPEN, horizontal_kernel, iterations=1)
    line_mask |= cv2.morphologyEx(binary, cv2.MORPH_OPEN, vertical_kernel, iterations=1)
    line_mask = cv2.dilate(line_mask, np.ones((3, 3), np.uint8), iterations=1)
    contours, _ = cv2.findContours(line_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates = []
    for contour in contours:
        area = cv2.contourArea(contour)
        _x, _y, w, h = cv2.boundingRect(contour)
        if area < 1000 or w < width * 0.25 or h < height * 0.12:
            continue
        candidates.append((area, contour))
    if not candidates:
        return image, None
    contour = max(candidates, key=lambda item: item[0])[1]
    boxes = [_expand_quad(np, _contour_quad(cv2, contour), image.shape)]
    line_box = _quad_from_border_lines(cv2, np, image)
    if line_box is not None:
        boxes.append(line_box)

    best = None
    for box in boxes:
        warped_candidate = _warp_quad(cv2, np, image, box)
        if warped_candidate.shape[0] > warped_candidate.shape[1]:
            warped_candidate = cv2.rotate(warped_candidate, cv2.ROTATE_90_COUNTERCLOCKWISE)
        candidate_grid = _detect_grid_lines(cv2, np, warped_candidate)
        score = _grid_score(candidate_grid, warped_candidate.shape)
        option = (score, warped_candidate, box)
        if best is None or option[0] > best[0]:
            best = option

    _score, warped, quad = best
    if warped.shape[0] > warped.shape[1]:
        warped = cv2.rotate(warped, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return warped, quad.astype(int).tolist()


def _make_cells(x_lines, y_lines, min_width=18, min_height=12):
    cells = []
    for row, (y1, y2) in enumerate(zip(y_lines, y_lines[1:])):
        if y2 - y1 < min_height:
            continue
        for col, (x1, x2) in enumerate(zip(x_lines, x_lines[1:])):
            if x2 - x1 < min_width:
                continue
            cells.append({"row": row, "col": col, "bbox": (x1, y1, x2, y2), "text": "", "score": None})
    return cells


def _line_masks(cv2, np, image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    binary = cv2.adaptiveThreshold(
        blurred, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 12)
    height, width = binary.shape
    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(width // 35, 25), 1))
    vertical_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(height // 35, 25)))
    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, horizontal_kernel, iterations=1)
    vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN, vertical_kernel, iterations=1)
    return horizontal, vertical


def _polyline_from_mask(np, mask, fixed_positions, vertical=True, band=8, step=18, min_points=3):
    height, width = mask.shape
    lines = []
    for pos in fixed_positions:
        points = []
        if vertical:
            for y1 in range(0, height, step):
                y2 = min(y1 + step, height)
                x1 = max(0, int(pos) - band)
                x2 = min(width, int(pos) + band + 1)
                ys, xs = np.nonzero(mask[y1:y2, x1:x2])
                if len(xs) > 0:
                    points.append((int(round(x1 + float(np.mean(xs)))), int(round(y1 + float(np.mean(ys))))))
            lines.append(points if len(points) >= min_points else [(int(pos), 0), (int(pos), height - 1)])
        else:
            for x1 in range(0, width, step):
                x2 = min(x1 + step, width)
                y1 = max(0, int(pos) - band)
                y2 = min(height, int(pos) + band + 1)
                ys, xs = np.nonzero(mask[y1:y2, x1:x2])
                if len(xs) > 0:
                    points.append((int(round(x1 + float(np.mean(xs)))), int(round(y1 + float(np.mean(ys))))))
            lines.append(points if len(points) >= min_points else [(0, int(pos)), (width - 1, int(pos))])
    return lines


def _trace_grid_curves(cv2, np, image, grid, band=8, step=18):
    horizontal_mask, vertical_mask = _line_masks(cv2, np, image)
    return {
        "vertical": _polyline_from_mask(np, vertical_mask, grid["x_lines"], vertical=True, band=band, step=step),
        "horizontal": _polyline_from_mask(np, horizontal_mask, grid["y_lines"], vertical=False, band=band, step=step),
    }


def _curve_value(points, axis, value):
    if not points:
        return 0.0
    pts = sorted(points, key=lambda item: item[axis])
    if value <= pts[0][axis]:
        return float(pts[0][1 - axis])
    if value >= pts[-1][axis]:
        return float(pts[-1][1 - axis])
    for left, right in zip(pts, pts[1:]):
        if left[axis] <= value <= right[axis]:
            span = max(float(right[axis] - left[axis]), 1.0)
            ratio = (value - left[axis]) / span
            return float(left[1 - axis] + (right[1 - axis] - left[1 - axis]) * ratio)
    return float(pts[-1][1 - axis])


def _curve_intersection(vertical_points, horizontal_points, nominal_x, nominal_y):
    x = _curve_value(vertical_points, 1, nominal_y)
    y = _curve_value(horizontal_points, 0, nominal_x)
    return x, y


def _make_curved_cells(x_lines, y_lines, curves, min_width=18, min_height=12, padding=2):
    if len(curves["vertical"]) != len(x_lines) or len(curves["horizontal"]) != len(y_lines):
        return _make_cells(x_lines, y_lines, min_width=min_width, min_height=min_height)

    cells = []
    for row, (y1, y2) in enumerate(zip(y_lines, y_lines[1:])):
        for col, (x1, x2) in enumerate(zip(x_lines, x_lines[1:])):
            points = [
                _curve_intersection(curves["vertical"][col], curves["horizontal"][row], x1, y1),
                _curve_intersection(curves["vertical"][col + 1], curves["horizontal"][row], x2, y1),
                _curve_intersection(curves["vertical"][col + 1], curves["horizontal"][row + 1], x2, y2),
                _curve_intersection(curves["vertical"][col], curves["horizontal"][row + 1], x1, y2),
            ]
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            bbox = (
                int(round(min(xs))) + padding,
                int(round(min(ys))) + padding,
                int(round(max(xs))) - padding,
                int(round(max(ys))) - padding,
            )
            if bbox[2] - bbox[0] < min_width or bbox[3] - bbox[1] < min_height:
                continue
            cells.append({
                "row": row,
                "col": col,
                "bbox": bbox,
                "polygon": [(int(round(x)), int(round(y))) for x, y in points],
                "text": "",
                "score": None,
            })
    return cells


def _box_center(box):
    xs = [point[0] for point in box]
    ys = [point[1] for point in box]
    return sum(xs) / len(xs), sum(ys) / len(ys)


def _assign_ocr_to_cells(cells, ocr_result):
    for box, text, score in ocr_result:
        cx, cy = _box_center(box)
        text = " ".join(str(text).split())
        if not text:
            continue
        for cell in cells:
            x1, y1, x2, y2 = cell["bbox"]
            if x1 <= cx <= x2 and y1 <= cy <= y2:
                if cell["text"]:
                    cell["text"] += " "
                cell["text"] += text
                cell["score"] = float(score) if cell["score"] is None else min(cell["score"], float(score))
                break
    return cells
