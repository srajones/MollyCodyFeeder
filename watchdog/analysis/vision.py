"""Bowl vision: daylight fill level, IR night-vision change detection, dog-at-bowl detection."""

from pathlib import Path
from typing import Optional, Tuple

import cv2
import numpy as np

from watchdog.analysis.results import VisualAnalysisResult


class VisionMixin:
    """Before/after bowl image analysis (daylight + IR)."""

    # Normalized (x1, y1, x2, y2) regions in the 720x1280 portrait frame where fill is MEASURED
    DAY_PIT = (0.32, 0.88, 0.75, 0.985)  # inner dish floor where kibble piles up (daylight darkness metric)
    IR_BAND = (0.22, 0.88, 0.88, 0.995)  # bottom band where pellets occlude the IR reflection (night)

    # -------------------------------------------------------------------------
    # Visual Analysis (Before vs. After Bowl Fill + IR Night Vision + Dog Detection)
    # -------------------------------------------------------------------------
    @staticmethod
    def _enhance_bowl_roi(roi_bgr: np.ndarray) -> np.ndarray:
        """
        Applies gamma shadow-lifting (gamma=0.42) + CLAHE local contrast enhancement
        so kibble inside the backlit stainless-steel bowl is clearly visible and measurable
        even when the background window is bright or IR night vision is active.
        """
        lut = np.array([((i / 255.0) ** 0.42) * 255 for i in range(256)], dtype=np.uint8)
        lifted = cv2.LUT(roi_bgr, lut)
        lab = cv2.cvtColor(lifted, cv2.COLOR_BGR2LAB)
        clahe = cv2.createCLAHE(clipLimit=3.5, tileGridSize=(6, 6))
        lab[:, :, 0] = clahe.apply(lab[:, :, 0])
        return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

    @staticmethod
    def _detect_infrared_mode(img_bgr: np.ndarray) -> bool:
        """Detects IR Night Vision mode (near-zero color saturation across the frame)."""
        hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
        return float(np.mean(hsv[:, :, 1])) < 14.0

    @staticmethod
    def _detect_dog_at_bowl(
        after_img: np.ndarray,
        before_img: Optional[np.ndarray] = None,
    ) -> bool:
        """
        True if something (the dog's head) appeared over the bowl between the two frames.
        Only the zone directly over/around the bowl is compared (a dog lying in the room behind is not
        "at the bowl"), brightness is normalized, and a lights-on / IR switch is not mistaken for a dog.
        """
        if before_img is None or before_img.shape != after_img.shape:
            return False
        h, w = after_img.shape[:2]
        y1, y2, x1, x2 = int(0.74 * h), int(0.99 * h), int(0.08 * w), int(0.92 * w)

        def zone(img):
            g = cv2.GaussianBlur(cv2.cvtColor(img[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY), (9, 9), 0).astype(np.float32)
            return g / (float(np.median(g)) + 1.0)

        sat_b = float(np.mean(cv2.cvtColor(before_img, cv2.COLOR_BGR2HSV)[:, :, 1]))
        sat_a = float(np.mean(cv2.cvtColor(after_img, cv2.COLOR_BGR2HSV)[:, :, 1]))
        if (sat_b < 14.0) != (sat_a < 14.0):
            return False  # IR <-> daylight switch: frames aren't comparable
        diff = np.abs(zone(after_img) - zone(before_img))
        return float(np.mean(diff > 0.35)) >= 0.30

    @staticmethod
    def _ir_change_metrics(before_img: np.ndarray, after_img: np.ndarray) -> Tuple[float, float]:
        """
        IR night vision: the steel bowl floor shows a bright IR reflection and new kibble appears as
        pellet silhouettes cutting into it. Absolute darkness is meaningless at night, so measure
        exposure-normalized change in the bottom band: (fraction that got darker, hotspot occlusion).
        """
        h, w = after_img.shape[:2]

        def band(img):
            g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
            x1, y1, x2, y2 = VisionMixin.IR_BAND
            b = cv2.GaussianBlur(g[int(y1 * h) : int(y2 * h), int(x1 * w) : int(x2 * w)], (5, 5), 0)
            return b / (float(np.median(b)) + 1.0)

        def hotspot(img):
            g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            b = g[int(0.93 * h) : int(0.995 * h), int(0.30 * w) : int(0.80 * w)]
            return float(np.mean(b > 0.85 * np.percentile(b, 99.5)))

        darker = float(np.mean((band(before_img) - band(after_img)) > 0.25))
        hb, ha = hotspot(before_img), hotspot(after_img)
        occlusion = (hb - ha) / hb if hb >= 0.05 else 0.0
        return darker, float(occlusion)

    @staticmethod
    def _lit_frame(img: np.ndarray) -> bool:
        """Room lights on (colour frame), as opposed to IR night vision or a dim backlit scene."""
        return float(np.mean(cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[:, :, 1])) >= 35.0

    @staticmethod
    def _kibble_texture(img: np.ndarray) -> float:
        """Share of the bowl floor with pellet-like local contrast (a kibble pile is lumpy, steel is smooth)."""
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
        """Plain-language camera verdict (pure). Vision is secondary to sound, so it says when it can't judge.

        In IR night vision the daylight fill metric is meaningless (reads ~0 % for every frame), so no fill
        percentages are ever printed at night — only the IR change metric that actually drove the verdict.
        """
        # The "dog at bowl" detector was wrong 14/15 times on the owner's labels (v0.6): it only means the
        # before/after frames differ a lot, so never claim a dog in the message.
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
            return (f"No clear new kibble seen (fill {before_fill * 100:.1f}% -> {after_fill * 100:.1f}%); "
                    "camera can't tell, sound decides.")
        return f"Camera can't tell if there is food ({fill_txt}); sound decides."

    def _compute_roi_kibble_fill(
        self, roi_bgr: np.ndarray, is_ir: bool = False, full_img_bgr: Optional[np.ndarray] = None
    ) -> Tuple[float, np.ndarray, np.ndarray]:
        enhanced = self._enhance_bowl_roi(roi_bgr)
        enh_gray = cv2.cvtColor(enhanced, cv2.COLOR_BGR2GRAY)

        if full_img_bgr is not None:
            ih, iw = full_img_bgr.shape[:2]
            # Inner stainless-steel dish floor where kibble piles up (excludes upper metallic rim)
            dx1, dy1, dx2, dy2 = self.DAY_PIT
            px1, py1 = int(dx1 * iw), int(dy1 * ih)
            px2, py2 = int(dx2 * iw), int(dy2 * ih)
            pit_gray = cv2.GaussianBlur(
                cv2.cvtColor(full_img_bgr[py1:py2, px1:px2], cv2.COLOR_BGR2GRAY),
                (7, 7),
                0,
            )
            # In daylight/indoor lighting, dark kibble pellets in the pit have gray < 6.5
            # above the static lower rim shadow baseline (~13.0%)
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
        """True if food is visible in the bowl in a single snapshot; None = can't tell (never 'empty')."""
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
        """(inner-bowl kibble fill ratio, is_ir). Fill is None at night (not measurable in IR)."""
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
        # Vision may CONFIRM food but never claims "empty": the darkness metric reads 0 % for a full bowl
        # with room lights on (light-brown kibble on bright steel), which used to veto good feeds.
        bowl_empty = False
        ir_food_seen = False
        ir_darker, ir_occlusion = 0.0, 0.0
        if is_ir:
            # The daylight darkness metric reads ~0 % for every IR frame, which used to flag EVERY
            # night feed as an empty bowl. At night: never claim "empty" from vision; only confirm
            # new kibble from before->after change, otherwise leave the verdict to the audio.
            bowl_empty = False
            if has_before and not dog_at_bowl:
                ir_darker, ir_occlusion = self._ir_change_metrics(before_img, after_img)
                frame_diff_ratio = ir_darker
                ir_food_seen = ir_darker >= 0.05 or ir_occlusion >= 0.30
        # Food visibly accumulated only if NOT obstructed (`not dog_at_bowl`) and inner dish pit grew darker/fuller
        food_increased = bool(
            (not dog_at_bowl)
            and has_before
            and after_fill >= self.cfg.min_bowl_fill_ratio
            and (fill_delta >= 0.025 or (fill_delta >= 0.010 and pit_darker_ratio >= 0.18))
        )
        if is_ir:
            food_increased = ir_food_seen
        # "Food seen in the bowl" (dog is OK for now): dark kibble measured, new kibble seen, or a clearly
        # textured kibble pile in a lit (colour) frame. False means "can't tell", never "empty".
        food_present_cv = bool(
            food_increased
            or (not is_ir and after_fill >= self.cfg.min_bowl_fill_ratio)
            or (not is_ir and self._lit_frame(after_img) and self._kibble_texture(after_img) >= 0.33)
        )

        if not annotated_output_path:
            annotated_output_path = str(Path(after_image_path).with_name("annotated_bowl.jpg"))

        annotated = after_img.copy()
        # Overlay the shadow-enhanced bowl ROI so kibble is clearly visible to the user
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
        # Thin yellow box = the exact region the fill metric is measured in
        mx1, my1, mx2, my2 = self.IR_BAND if is_ir else self.DAY_PIT
        cv2.rectangle(annotated, (int(mx1 * w), int(my1 * h)), (int(mx2 * w), int(my2 * h) - 1), (0, 255, 255), 1)
        tags = (" [IR]" if is_ir else "") + (" [DOG]" if dog_at_bowl else "")
        fill_txt = ""
        status_label = f"{fill_txt}{verdict}{tags}"
        # Banner at the TOP of the frame so it never covers the kibble in the bowl
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
