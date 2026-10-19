"""Timestamp conversion shared by frame and transcoded-chunk extraction."""


def rounded_frame_seek_seconds(pts_ms: int) -> str:
    """Seek from the lower edge of a rounded millisecond's half-ms interval.

    Seeking at the rounded value can discard the intended frame when its real
    PTS falls just before that value, including the final frame of a video.
    """
    return f"{max(0, pts_ms - 0.5) / 1000:.6f}"
