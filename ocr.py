"""文字识别（RapidOCR，模型随包自带，离线可用）。用来读种子数量。"""
import re

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr import RapidOCR   # 导入较慢，用到时再加载
        _engine = RapidOCR()
    return _engine


def read_words(img_bgr):
    """返回 [(文字, 中心x, 中心y, 置信度)]。"""
    r = _get_engine()(img_bgr)
    if not r.txts:
        return []
    out = []
    for text, score, box in zip(r.txts, r.scores, r.boxes):
        cx, cy = box.mean(axis=0)
        out.append((text.strip(), float(cx), float(cy), float(score)))
    return out


def to_int(text):
    t = text.replace(" ", "").replace("O", "0").replace("o", "0").replace("l", "1").replace("I", "1")
    return int(t) if re.fullmatch(r"\d+", t) else None
