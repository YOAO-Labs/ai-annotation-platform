import type { PetMood } from "./petLines";
import styles from "./pet.module.css";

const BODY = "#8b5cf6";
const BODY_DARK = "#6d28d9";
const EYE_WHITE = "#ffffff";
const EYE_DARK = "#312e81";
const BLUSH = "#f9a8d4";
const MOUTH = "#4c1d95";
const SPARKLE = "#fbbf24";
const HILIGHT = "#c4b5fd";

interface PixelSpriteProps {
  mood: PetMood;
  size?: number;
}

export function PixelSprite({ mood, size = 56 }: PixelSpriteProps) {
  const happy = mood === "happy" || mood === "celebrate";
  const celebrate = mood === "celebrate";

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      className={styles.sprite}
      aria-hidden="true"
      role="img"
    >
      <ellipse cx="12" cy="21.5" rx="6" ry="1.4" fill={BODY_DARK} opacity="0.25" />

      <path
        d="M12 3.5C7.6 3.5 4.5 7 4.5 11.8c0 4.6 2.9 8 7.5 8s7.5-3.4 7.5-8C19.5 7 16.4 3.5 12 3.5Z"
        fill={BODY}
        stroke={BODY_DARK}
        strokeWidth="1"
      />
      <ellipse cx="9" cy="8" rx="1.6" ry="1" fill={HILIGHT} opacity="0.7" />

      {happy && (
        <>
          <circle cx="7.4" cy="14" r="1.2" fill={BLUSH} opacity="0.85" />
          <circle cx="16.6" cy="14" r="1.2" fill={BLUSH} opacity="0.85" />
        </>
      )}

      {happy ? (
        <g stroke={EYE_DARK} strokeWidth="1.4" strokeLinecap="round" fill="none">
          <path d="M8 12c.7-1.1 2-1.1 2.7 0" />
          <path d="M13.3 12c.7-1.1 2-1.1 2.7 0" />
        </g>
      ) : (
        <g className={styles.eyes}>
          <circle cx="9.4" cy="11.4" r="2.1" fill={EYE_WHITE} />
          <circle cx="14.6" cy="11.4" r="2.1" fill={EYE_WHITE} />
          <circle cx="9.9" cy="11.7" r="1.05" fill={EYE_DARK} />
          <circle cx="15.1" cy="11.7" r="1.05" fill={EYE_DARK} />
        </g>
      )}

      {happy ? (
        <path d="M10 14.6c1.2 1.6 2.8 1.6 4 0Z" fill={MOUTH} />
      ) : (
        <path
          d="M10.6 15c.9.7 1.9.7 2.8 0"
          stroke={MOUTH}
          strokeWidth="1.1"
          strokeLinecap="round"
          fill="none"
        />
      )}

      {celebrate && (
        <g fill={SPARKLE}>
          <path className={styles.sparkle} d="M4 5l.6 1.4L6 7l-1.4.6L4 9l-.6-1.4L2 7l1.4-.6Z" />
          <path className={styles.sparkle} d="M20 4l.6 1.4L22 6l-1.4.6L20 8l-.6-1.4L18 6l1.4-.6Z" />
          <path
            className={styles.sparkle}
            d="M19 16l.5 1.1L20.6 18l-1.1.5L19 19.6l-.5-1.1L17.4 18l1.1-.4Z"
          />
        </g>
      )}
    </svg>
  );
}
