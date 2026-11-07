"""Bowl vision: daylight fill level, IR night-vision change detection, dog-at-bowl detection."""
from __future__ import annotations

import builtins
import io
import sys
import types
from pathlib import Path
from typing import Any, Optional, Tuple

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore[assignment]

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]

from custom_components.molly_cody_feeder.core.analysis.results import (
    VisualAnalysisResult,
)


def _build_cv2_compat() -> types.ModuleType:
    mod = types.ModuleType("cv2")
    mod.IMREAD_COLOR = 1
    mod.IMWRITE_JPEG_QUALITY = 1
    mod.COLOR_BGR2RGB = 4
    mod.COLOR_RGB2BGR = 4
    mod.COLOR_BGR2GRAY = 6
    mod.COLOR_BGR2HSV = 40
    mod.COLOR_BGR2LAB = 44
    mod.COLOR_LAB2BGR = 56
    mod.FONT_HERSHEY_SIMPLEX = 0

    def imread(path: str, flags: int = 1) -> Optional[np.ndarray]:
        try:
            with Image.open(path) as im:
                rgb = np.asarray(im.convert("RGB"), dtype=np.uint8)
                return np.ascontiguousarray(rgb[:, :, ::-1])
        except Exception:
            return None

    def imwrite(path: str, img_bgr: np.ndarray) -> bool:
        try:
            rgb = np.ascontiguousarray(img_bgr[:, :, ::-1]) if img_bgr.ndim == 3 else img_bgr
            Image.fromarray(rgb).save(path, quality=92)
            return True
        except Exception:
            return False

    def imencode(ext: str, img_bgr: np.ndarray, params: Any = None) -> Tuple[bool, np.ndarray]:
        try:
            quality = 90
            if params and len(params) >= 2 and params[0] == mod.IMWRITE_JPEG_QUALITY:
                quality = int(params[1])
            rgb = np.ascontiguousarray(img_bgr[:, :, ::-1]) if img_bgr.ndim == 3 else img_bgr
            buf = io.BytesIO()
            fmt = "JPEG" if ext.lower() in (".jpg", ".jpeg") else "PNG"
            Image.fromarray(rgb).save(buf, format=fmt, quality=quality)
            return True, np.frombuffer(buf.getvalue(), dtype=np.uint8)
        except Exception:
            return False, np.zeros(0, dtype=np.uint8)

    def imdecode(buf: np.ndarray, flags: int = 1) -> Optional[np.ndarray]:
        try:
            with Image.open(io.BytesIO(bytes(buf))) as im:
                rgb = np.asarray(im.convert("RGB"), dtype=np.uint8)
                return np.ascontiguousarray(rgb[:, :, ::-1])
        except Exception:
            return None

    def LUT(src: np.ndarray, lut: np.ndarray) -> np.ndarray:
        return np.asarray(lut, dtype=src.dtype)[src]

    def cvtColor(src: np.ndarray, code: int) -> np.ndarray:
        if code == mod.COLOR_BGR2GRAY:
            b = src[:, :, 0].astype(np.uint32)
            g = src[:, :, 1].astype(np.uint32)
            r = src[:, :, 2].astype(np.uint32)
            return ((1868 * b + 9617 * g + 4899 * r + 8192) >> 14).astype(np.uint8)
        if code == mod.COLOR_BGR2RGB:
            return np.ascontiguousarray(src[:, :, ::-1])
        if code == mod.COLOR_BGR2HSV:
            rgb = np.ascontiguousarray(src[:, :, ::-1])
            hsv = np.asarray(Image.fromarray(rgb).convert("HSV"), dtype=np.uint8).copy()
            hsv[:, :, 0] = (hsv[:, :, 0].astype(np.uint16) * 180 // 255).astype(np.uint8)
            return hsv
        if code == mod.COLOR_BGR2LAB:
            rgb = np.ascontiguousarray(src[:, :, ::-1])
            return np.asarray(Image.fromarray(rgb).convert("YCbCr"), dtype=np.uint8).copy()
        if code == mod.COLOR_LAB2BGR:
            rgb = np.asarray(Image.fromarray(src, mode="YCbCr").convert("RGB"), dtype=np.uint8)
            return np.ascontiguousarray(rgb[:, :, ::-1])
        return src.copy()

    def _gaussian_kernel_1d(k: int, sigma: float) -> np.ndarray:
        if k <= 1:
            return np.array([1.0], dtype=np.float32)
        if sigma <= 0:
            sigma = 0.3 * ((k - 1) * 0.5 - 1.0) + 0.8
        x = np.arange(k, dtype=np.float32) - (k - 1) * 0.5
        g = np.exp(-(x * x) / (2.0 * sigma * sigma))
        return (g / g.sum()).astype(np.float32)

    def _sep_filter2d(src: np.ndarray, kx: np.ndarray, ky: np.ndarray) -> np.ndarray:
        arr = src.astype(np.float32)
        h, w = arr.shape[:2]
        px, py = len(kx) // 2, len(ky) // 2
        if px > 0:
            pad_spec = ((0, 0), (px, px)) if arr.ndim == 2 else ((0, 0), (px, px), (0, 0))
            padded = np.pad(arr, pad_spec, mode="reflect")
            arr = sum(kx[i] * padded[:, i : i + w] for i in range(len(kx)))
        if py > 0:
            pad_spec = ((py, py), (0, 0)) if arr.ndim == 2 else ((py, py), (0, 0), (0, 0))
            padded = np.pad(arr, pad_spec, mode="reflect")
            arr = sum(ky[i] * padded[i : i + h, :] for i in range(len(ky)))
        if src.dtype == np.uint8:
            return np.clip(np.round(arr), 0, 255).astype(np.uint8)
        return arr.astype(src.dtype)

    def GaussianBlur(src: np.ndarray, ksize: Tuple[int, int], sigmaX: float, sigmaY: float = 0.0) -> np.ndarray:
        kx = _gaussian_kernel_1d(int(ksize[0]), float(sigmaX))
        ky = _gaussian_kernel_1d(int(ksize[1]), float(sigmaY if sigmaY > 0 else sigmaX))
        return _sep_filter2d(src, kx, ky)

    def blur(src: np.ndarray, ksize: Tuple[int, int]) -> np.ndarray:
        nx, ny = max(1, int(ksize[0])), max(1, int(ksize[1]))
        kx = np.full(nx, 1.0 / nx, dtype=np.float32)
        ky = np.full(ny, 1.0 / ny, dtype=np.float32)
        return _sep_filter2d(src, kx, ky)

    class _CLAHE:
        def __init__(self, clipLimit: float = 2.0, tileGridSize: Tuple[int, int] = (8, 8)) -> None:
            self.clip_limit = float(clipLimit)
            self.grid_x = max(1, int(tileGridSize[0]))
            self.grid_y = max(1, int(tileGridSize[1]))

        def apply(self, gray: np.ndarray) -> np.ndarray:
            h, w = gray.shape[:2]
            ty = max(1, (h + self.grid_y - 1) // self.grid_y)
            tx = max(1, (w + self.grid_x - 1) // self.grid_x)
            luts = np.zeros((self.grid_y, self.grid_x, 256), dtype=np.float32)
            for gy in range(self.grid_y):
                y0, y1 = gy * ty, min(h, (gy + 1) * ty)
                if y1 <= y0:
                    y0, y1 = max(0, h - ty), h
                for gx in range(self.grid_x):
                    x0, x1 = gx * tx, min(w, (gx + 1) * tx)
                    if x1 <= x0:
                        x0, x1 = max(0, w - tx), w
                    tile = gray[y0:y1, x0:x1]
                    n_pix = max(1, tile.size)
                    hist = np.bincount(tile.ravel(), minlength=256).astype(np.float32)
                    limit = max(1.0, self.clip_limit * n_pix / 256.0)
                    excess = np.maximum(0.0, hist - limit)
                    hist = np.minimum(hist, limit) + (excess.sum() / 256.0)
                    cdf = np.cumsum(hist)
                    luts[gy, gx] = np.clip((cdf - cdf[0]) * 255.0 / max(1.0, n_pix - cdf[0]), 0.0, 255.0)
            yy = np.clip((np.arange(h, dtype=np.float32) + 0.5) / ty - 0.5, 0.0, self.grid_y - 1.0)
            xx = np.clip((np.arange(w, dtype=np.float32) + 0.5) / tx - 0.5, 0.0, self.grid_x - 1.0)
            gy0 = np.floor(yy).astype(np.int32)
            gx0 = np.floor(xx).astype(np.int32)
            gy1 = np.minimum(gy0 + 1, self.grid_y - 1)
            gx1 = np.minimum(gx0 + 1, self.grid_x - 1)
            wy = (yy - gy0)[:, None]
            wx = (xx - gx0)[None, :]
            v00 = luts[gy0[:, None], gx0[None, :], gray]
            v01 = luts[gy0[:, None], gx1[None, :], gray]
            v10 = luts[gy1[:, None], gx0[None, :], gray]
            v11 = luts[gy1[:, None], gx1[None, :], gray]
            out = (1.0 - wy) * ((1.0 - wx) * v00 + wx * v01) + wy * ((1.0 - wx) * v10 + wx * v11)
            return np.clip(np.round(out), 0, 255).astype(np.uint8)

    def createCLAHE(clipLimit: float = 2.0, tileGridSize: Tuple[int, int] = (8, 8)) -> _CLAHE:
        return _CLAHE(clipLimit=clipLimit, tileGridSize=tileGridSize)

    def Canny(gray: np.ndarray, threshold1: float, threshold2: float) -> np.ndarray:
        g = gray.astype(np.float32)
        p = np.pad(g, ((1, 1), (1, 1)), mode="reflect")
        gx = (p[:-2, 2:] + 2.0 * p[1:-1, 2:] + p[2:, 2:]) - (p[:-2, :-2] + 2.0 * p[1:-1, :-2] + p[2:, :-2])
        gy = (p[2:, :-2] + 2.0 * p[2:, 1:-1] + p[2:, 2:]) - (p[:-2, :-2] + 2.0 * p[:-2, 1:-1] + p[:-2, 2:])
        mag = np.hypot(gx, gy)
        return np.where(mag >= float(threshold2), 255, 0).astype(np.uint8)

    def dilate(src: np.ndarray, kernel: np.ndarray, iterations: int = 1) -> np.ndarray:
        kh, kw = kernel.shape[:2] if kernel is not None else (3, 3)
        ph, pw = kh // 2, kw // 2
        out = src.copy()
        for _ in range(max(1, int(iterations))):
            p = np.pad(out, ((ph, ph), (pw, pw)), mode="edge")
            h, w = out.shape[:2]
            acc = np.zeros_like(out)
            for dy in range(kh):
                for dx in range(kw):
                    acc = np.maximum(acc, p[dy : dy + h, dx : dx + w])
            out = acc
        return out

    def rectangle(
        img: np.ndarray,
        pt1: Tuple[int, int],
        pt2: Tuple[int, int],
        color: Tuple[int, int, int],
        thickness: int = 1,
    ) -> np.ndarray:
        h, w = img.shape[:2]
        x1, y1 = max(0, min(w - 1, int(pt1[0]))), max(0, min(h - 1, int(pt1[1])))
        x2, y2 = max(0, min(w, int(pt2[0]))), max(0, min(h, int(pt2[1])))
        if x2 <= x1 or y2 <= y1:
            return img
        c = np.array(color[:3], dtype=np.uint8)
        if thickness < 0:
            img[y1:y2, x1:x2] = c
        else:
            t = max(1, int(thickness))
            img[y1 : min(y2, y1 + t), x1:x2] = c
            img[max(y1, y2 - t) : y2, x1:x2] = c
            img[y1:y2, x1 : min(x2, x1 + t)] = c
            img[y1:y2, max(x1, x2 - t) : x2] = c
        return img

    def getTextSize(text: str, fontFace: int, fontScale: float, thickness: int) -> Tuple[Tuple[int, int], int]:
        return (max(10, int(len(text) * 11 * fontScale)), max(12, int(22 * fontScale))), 4

    def putText(
        img: np.ndarray,
        text: str,
        org: Tuple[int, int],
        fontFace: int,
        fontScale: float,
        color: Tuple[int, int, int],
        thickness: int = 1,
    ) -> np.ndarray:
        try:
            pil_im = Image.fromarray(np.ascontiguousarray(img[:, :, ::-1]))
            draw = ImageDraw.Draw(pil_im)
            rgb_color = (int(color[2]), int(color[1]), int(color[0]))
            draw.text((int(org[0]), max(0, int(org[1]) - 14)), text, fill=rgb_color)
            img[:] = np.asarray(pil_im, dtype=np.uint8)[:, :, ::-1]
        except Exception:
            pass
        return img

    mod.imread = imread  # type: ignore[attr-defined]
    mod.imwrite = imwrite  # type: ignore[attr-defined]
    mod.imencode = imencode  # type: ignore[attr-defined]
    mod.imdecode = imdecode  # type: ignore[attr-defined]
    mod.LUT = LUT  # type: ignore[attr-defined]
    mod.cvtColor = cvtColor  # type: ignore[attr-defined]
    mod.GaussianBlur = GaussianBlur  # type: ignore[attr-defined]
    mod.blur = blur  # type: ignore[attr-defined]
    mod.createCLAHE = createCLAHE  # type: ignore[attr-defined]
    mod.Canny = Canny  # type: ignore[attr-defined]
    mod.dilate = dilate  # type: ignore[attr-defined]
    mod.rectangle = rectangle  # type: ignore[attr-defined]
    mod.getTextSize = getTextSize  # type: ignore[attr-defined]
    mod.putText = putText  # type: ignore[attr-defined]
    return mod


try:
    import cv2  # type: ignore[import-not-found]
except ImportError:
    cv2 = _build_cv2_compat()
    sys.modules.setdefault("cv2", cv2)

setattr(builtins, "cv2", cv2)


class VisionMixin:
    """Before/after bowl image analysis (daylight + IR)."""

    DAY_PIT = (0.32, 0.88, 0.75, 0.985)
    IR_BAND = (0.22, 0.88, 0.88, 0.995)

    @staticmethod
    def _enhance_bowl_roi(roi_bgr: np.ndarray) -> np.ndarray:
        lut = np.array([((i / 255.0) ** 0.42) * 255 for i in range(256)], dtype=np.uint8)
        lifted = cv2.LUT(roi_bgr, lut)
        lab = cv2.cvtColor(lifted, cv2.COLOR_BGR2LAB)
        clahe = cv2.createCLAHE(clipLimit=3.5, tileGridSize=(6, 6))
        lab[:, :, 0] = clahe.apply(lab[:, :, 0])
        return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

    @staticmethod
    def _detect_infrared_mode(img_bgr: np.ndarray) -> bool:
        hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
        return float(np.mean(hsv[:, :, 1])) < 14.0

    @staticmethod
    def _detect_dog_at_bowl(
        after_img: np.ndarray,
        before_img: Optional[np.ndarray] = None,
    ) -> bool:
        if before_img is None or before_img.shape != after_img.shape:
            return False
        h, w = after_img.shape[:2]
        y1, y2, x1, x2 = int(0.74 * h), int(0.99 * h), int(0.08 * w), int(0.92 * w)

        def zone(img: np.ndarray) -> np.ndarray:
            g = cv2.GaussianBlur(cv2.cvtColor(img[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY), (9, 9), 0).astype(np.float32)
            return g / (float(np.median(g)) + 1.0)

        sat_b = float(np.mean(cv2.cvtColor(before_img, cv2.COLOR_BGR2HSV)[:, :, 1]))
        sat_a = float(np.mean(cv2.cvtColor(after_img, cv2.COLOR_BGR2HSV)[:, :, 1]))
        if (sat_b < 14.0) != (sat_a < 14.0):
            return False
        diff = np.abs(zone(after_img) - zone(before_img))
        return float(np.mean(diff > 0.35)) >= 0.30

    @staticmethod
    def _ir_change_metrics(before_img: np.ndarray, after_img: np.ndarray) -> Tuple[float, float]:
        h, w = after_img.shape[:2]

        def band(img: np.ndarray) -> np.ndarray:
            g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
            x1, y1, x2, y2 = VisionMixin.IR_BAND
            b = cv2.GaussianBlur(g[int(y1 * h) : int(y2 * h), int(x1 * w) : int(x2 * w)], (5, 5), 0)
            return b / (float(np.median(b)) + 1.0)

        def hotspot(img: np.ndarray) -> float:
            g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            b = g[int(0.93 * h) : int(0.995 * h), int(0.30 * w) : int(0.80 * w)]
            return float(np.mean(b > 0.85 * np.percentile(b, 99.5)))

        darker = float(np.mean((band(before_img) - band(after_img)) > 0.25))
        hb, ha = hotspot(before_img), hotspot(after_img)
        occlusion = (hb - ha) / hb if hb >= 0.05 else 0.0
        return darker, float(occlusion)

    @staticmethod
    def _lit_frame(img: np.ndarray) -> bool:
        return float(np.mean(cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[:, :, 1])) >= 35.0

    @staticmethod
    def _kibble_texture(img: np.ndarray) -> float:
        h, w = img.shape[:2]
        g = cv2.cvtColor(img[int(0.925 * h) : int(0.995 * h), int(0.14 * w) : int(0.88 * w)], cv2.COLOR_BGR2GRAY)
        g = cv2.GaussianBlur(cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 8)).apply(g), (3, 3), 0).astype(np.float32)
        mean = cv2.blur(g, (15, 15))
        sd = np.sqrt(np.maximum(cv2.blur(g * g, (15, 15)) - mean * mean, 0))
        return float(np.mean(sd > 14))

    @staticmethod
    def _visual_diagnosis(
        is_ir: bool,
        has_before: bool,
        dog_at_bowl: bool,
        bowl_empty: bool,
        food_increased: bool,
        food_present_cv: bool,
        before_fill: float,
        after_fill: float,
        fill_delta: float,
        ir_darker: float = 0.0,
        ir_occlusion: float = 0.0,
    ) -> str:
        if is_ir:
            if dog_at_bowl:
                return "IR night vision: the before/after photos differ a lot (lighting/motion); can't judge the bowl, sound decides."
            if food_increased:
                if ir_darker >= 0.05:
                    return f"IR night vision: new kibble seen (bowl floor darker in {ir_darker * 100:.0f}% of the area)."
                return f"IR night vision: new kibble seen (bowl-floor reflection {ir_occlusion * 100:.0f}% covered)."
            return "IR night vision: can't judge the bowl at night; sound decides."
        fill_txt = f"fill {after_fill * 100:.1f}%"
        if dog_at_bowl:
            return "The before/after photos differ a lot (lighting/motion): can't judge the bowl; sound decides."
        if bowl_empty:
            return f"Bowl looks empty ({fill_txt})."
        if has_before and food_increased:
            return f"New kibble seen in the bowl (fill {before_fill * 100:.1f}% -> {after_fill * 100:.1f}%)."
        if food_present_cv:
            if has_before:
                return f"Food in the bowl ({fill_txt}), but no clear increase seen (change {fill_delta * 100:+.1f}%)."
            return f"Food visible in the bowl ({fill_txt})."
        if has_before:
            return (
                f"No clear new kibble seen (fill {before_fill * 100:.1f}% -> {after_fill * 100:.1f}%); "
                "camera can't tell, sound decides."
            )
        return f"Camera can't tell if there is food ({fill_txt}); sound decides."

    def _compute_roi_kibble_fill(
        self, roi_bgr: np.ndarray, is_ir: bool = False, full_img_bgr: Optional[np.ndarray] = None
    ) -> Tuple[float, np.ndarray, np.ndarray]:
        enhanced = self._enhance_bowl_roi(roi_bgr)
        enh_gray = cv2.cvtColor(enhanced, cv2.COLOR_BGR2GRAY)

        if full_img_bgr is not None:
            ih, iw = full_img_bgr.shape[:2]
            dx1, dy1, dx2, dy2 = self.DAY_PIT
            px1, py1 = int(dx1 * iw), int(dy1 * ih)
            px2, py2 = int(dx2 * iw), int(dy2 * ih)
            pit_gray = cv2.GaussianBlur(
                cv2.cvtColor(full_img_bgr[py1:py2, px1:px2], cv2.COLOR_BGR2GRAY),
                (7, 7),
                0,
            )
            raw_dark_ratio = float(np.mean(pit_gray < 6.5))
            fill_ratio = max(0.0, raw_dark_ratio - 0.130)
            return fill_ratio, enh_gray, enhanced

        hsv = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)

        v_max = 180 if is_ir else 165
        dark_and_mid_mask = (hsv[:, :, 2] > 18) & (hsv[:, :, 2] < v_max)
        edges = cv2.Canny(gray, 38 if is_ir else 45, 120 if is_ir else 130)
        kernel = np.ones((5, 5), np.uint8)
        textured_mask = cv2.dilate(edges, kernel, iterations=2) > 0

        kibble_mask = dark_and_mid_mask & textured_mask
        return float(np.mean(kibble_mask)), enh_gray, enhanced

    def food_seen(self, image_path: str) -> Optional[bool]:
        img = cv2.imread(image_path)
        if img is None or self._detect_infrared_mode(img):
            return None
        fill, _ = self.bowl_fill(image_path)
        if fill is not None and fill >= self.cfg.min_bowl_fill_ratio:
            return True
        if self._lit_frame(img) and self._kibble_texture(img) >= 0.33:
            return True
        return None

    def bowl_fill(self, image_path: str) -> Tuple[Optional[float], bool]:
        img = cv2.imread(image_path)
        if img is None:
            return None, False
        if self._detect_infrared_mode(img):
            return None, True
        h, w = img.shape[:2]
        x1_n, y1_n, x2_n, y2_n = self.cfg.bowl_roi
        roi = img[int(y1_n * h) : int(y2_n * h), int(x1_n * w) : int(x2_n * w)]
        fill, _, _ = self._compute_roi_kibble_fill(roi, is_ir=False, full_img_bgr=img)
        return float(fill), False

    def analyze_bowl_visual(
        self,
        after_image_path: str,
        before_image_path: Optional[str] = None,
        annotated_output_path: Optional[str] = None,
    ) -> VisualAnalysisResult:
        after_img = cv2.imread(after_image_path)
        if after_img is None:
            raise ValueError(f"Could not read image at {after_image_path}")

        h, w = after_img.shape[:2]
        is_ir = self._detect_infrared_mode(after_img)
        x1_n, y1_n, x2_n, y2_n = self.cfg.bowl_roi
        x1, y1, x2, y2 = int(x1_n * w), int(y1_n * h), int(x2_n * w), int(y2_n * h)
        after_roi = after_img[y1:y2, x1:x2]

        after_fill, after_gray, after_enhanced = self._compute_roi_kibble_fill(
            after_roi, is_ir=is_ir, full_img_bgr=after_img
        )
        before_fill = 0.0
        frame_diff_ratio = 0.0
        pit_darker_ratio = 0.0
        dog_at_bowl = False

        has_before = False
        if before_image_path and Path(before_image_path).exists():
            before_img = cv2.imread(before_image_path)
            if before_img is not None and before_img.shape == after_img.shape:
                has_before = True
                dog_at_bowl = self._detect_dog_at_bowl(after_img, before_img)
                before_roi = before_img[y1:y2, x1:x2]
                before_fill, before_gray, _ = self._compute_roi_kibble_fill(
                    before_roi,
                    is_ir=self._detect_infrared_mode(before_img),
                    full_img_bgr=before_img,
                )
                dx1, dy1, dx2, dy2 = self.DAY_PIT
                px1, py1, px2, py2 = int(dx1 * w), int(dy1 * h), int(dx2 * w), int(dy2 * h)
                pit_after_g = cv2.GaussianBlur(
                    cv2.cvtColor(after_img[py1:py2, px1:px2], cv2.COLOR_BGR2GRAY), (9, 9), 0
                ).astype(np.float32)
                pit_before_g = cv2.GaussianBlur(
                    cv2.cvtColor(before_img[py1:py2, px1:px2], cv2.COLOR_BGR2GRAY), (9, 9), 0
                ).astype(np.float32)
                pit_darker_ratio = float(np.mean((pit_before_g - pit_after_g) > 2.5))
                frame_diff_ratio = float(np.mean(np.abs(pit_before_g - pit_after_g) > 3.0))

        fill_delta = after_fill - before_fill if has_before else after_fill
        bowl_empty = False
        ir_food_seen = False
        ir_darker, ir_occlusion = 0.0, 0.0
        if is_ir:
            bowl_empty = False
            if has_before and not dog_at_bowl:
                ir_darker, ir_occlusion = self._ir_change_metrics(before_img, after_img)
                frame_diff_ratio = ir_darker
                ir_food_seen = ir_darker >= 0.05 or ir_occlusion >= 0.30

        food_increased = bool(
            (not dog_at_bowl)
            and has_before
            and after_fill >= self.cfg.min_bowl_fill_ratio
            and (fill_delta >= 0.025 or (fill_delta >= 0.010 and pit_darker_ratio >= 0.18))
        )
        if is_ir:
            food_increased = ir_food_seen

        food_present_cv = bool(
            food_increased
            or (not is_ir and after_fill >= self.cfg.min_bowl_fill_ratio)
            or (not is_ir and self._lit_frame(after_img) and self._kibble_texture(after_img) >= 0.33)
        )

        if not annotated_output_path:
            annotated_output_path = str(Path(after_image_path).with_name("annotated_bowl.jpg"))

        annotated = after_img.copy()
        annotated[y1:y2, x1:x2] = after_enhanced
        if food_increased:
            box_color, verdict = (0, 200, 0), "NEW FOOD SEEN"
        elif dog_at_bowl:
            box_color, verdict = (0, 190, 255), "DOG IN THE WAY"
        elif food_present_cv:
            box_color, verdict = (0, 200, 0), "FOOD IN BOWL"
        else:
            box_color, verdict = (0, 190, 255), "CAN'T TELL: SOUND DECIDES"
        cv2.rectangle(annotated, (x1, y1), (x2, y2), box_color, 3)
        mx1, my1, mx2, my2 = self.IR_BAND if is_ir else self.DAY_PIT
        cv2.rectangle(annotated, (int(mx1 * w), int(my1 * h)), (int(mx2 * w), int(my2 * h) - 1), (0, 255, 255), 1)
        tags = (" [IR]" if is_ir else "") + (" [DOG]" if dog_at_bowl else "")
        status_label = f"{verdict}{tags}"
        (tw, th), _ = cv2.getTextSize(status_label, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
        cv2.rectangle(annotated, (10, 12), (26 + tw, 24 + th + 8), (0, 0, 0), -1)
        cv2.putText(annotated, status_label, (18, 24 + th), cv2.FONT_HERSHEY_SIMPLEX, 0.7, box_color, 2)
        cv2.imwrite(annotated_output_path, annotated)

        food_present_llm = None
        llm_reasoning = ""
        if self.cfg.gemini_api_key:
            food_present_llm, llm_reasoning = self._verify_with_gemini(
                after_image_path, before_image_path if has_before else None
            )

        diagnosis = self._visual_diagnosis(
            is_ir=is_ir,
            has_before=has_before,
            dog_at_bowl=dog_at_bowl,
            bowl_empty=bowl_empty,
            food_increased=food_increased,
            food_present_cv=food_present_cv,
            before_fill=before_fill,
            after_fill=after_fill,
            fill_delta=fill_delta,
            ir_darker=ir_darker,
            ir_occlusion=ir_occlusion,
        )

        return VisualAnalysisResult(
            before_fill_ratio=round(before_fill, 4),
            after_fill_ratio=round(after_fill, 4),
            fill_delta_ratio=round(fill_delta, 4),
            frame_diff_ratio=round(frame_diff_ratio, 4),
            bowl_empty=bowl_empty,
            food_increased=food_increased,
            food_present_cv=food_present_cv,
            food_present_llm=food_present_llm,
            llm_reasoning=llm_reasoning,
            annotated_image_path=annotated_output_path,
            diagnosis=diagnosis,
            is_infrared_mode=is_ir,
            dog_at_bowl_detected=dog_at_bowl,
        )
