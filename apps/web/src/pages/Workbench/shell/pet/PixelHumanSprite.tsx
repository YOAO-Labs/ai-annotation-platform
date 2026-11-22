import type { PetMood } from "./petLines";
import styles from "./pet.module.css";

const HAIR = "#2f2a25";
const HAIR_LIGHT = "#4a3b31";
const SKIN = "#f4c7a1";
const SKIN_SHADE = "#d99b72";
const SHIRT = "#f8fafc";
const SHIRT_DARK = "#111827";
const APRON = "#18181b";
const APRON_LIGHT = "#e5e7eb";
const BOOT = "#030712";
const BOARD = "#f8fafc";
const BOARD_EDGE = "#334155";
const EYE = "#111827";
const MOUTH = "#7f1d1d";
const BLUSH = "#fda4af";
const SPARKLE = "#fbbf24";
const STATUS_BLUE = "#38bdf8";
const STATUS_AMBER = "#f59e0b";
const STATUS_RED = "#ef4444";
const STATUS_GREEN = "#22c55e";

interface PixelHumanSpriteProps {
  mood: PetMood;
  size?: number;
}

export function PixelHumanSprite({ mood, size = 56 }: PixelHumanSpriteProps) {
  const happy = mood === "happy" || mood === "celebrate";
  const celebrate = mood === "celebrate";
  const holding = mood === "holding" || mood === "multiSelected";
  const talking = mood === "idleTalk";
  const focused = mood === "aiRunning" || mood === "candidateReady" || mood === "selected";
  const alert = mood === "warning" || mood === "offline";
  const reviewing = mood === "review";
  const raisedArms = holding || celebrate;
  const bodyLift = happy ? -1 : mood === "offline" ? 1 : 0;

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 28 28"
      className={styles.sprite}
      data-pet-skin="pixel-human"
      aria-hidden="true"
      shapeRendering="crispEdges"
    >
      <rect x="9" y="25" width="10" height="1" fill={BOOT} opacity="0.16" />

      {holding && (
        <g>
          <rect x="5" y="1" width="18" height="5" fill={BOARD_EDGE} />
          <rect x="6" y="2" width="16" height="3" fill={BOARD} />
          <rect x="9" y="5" width="2" height="3" fill={BOARD_EDGE} />
          <rect x="17" y="5" width="2" height="3" fill={BOARD_EDGE} />
        </g>
      )}

      {mood === "aiRunning" && (
        <g fill={STATUS_BLUE}>
          <rect x="21" y="2" width="2" height="2" />
          <rect x="24" y="2" width="2" height="2" opacity="0.75" />
          <rect x="18" y="2" width="2" height="2" opacity="0.55" />
        </g>
      )}

      {mood === "candidateReady" && (
        <g fill={STATUS_GREEN}>
          <rect x="21" y="2" width="5" height="5" />
          <rect x="22" y="4" width="1" height="2" fill={BOARD} />
          <rect x="23" y="5" width="2" height="1" fill={BOARD} />
        </g>
      )}

      {alert && (
        <g fill={mood === "offline" ? STATUS_RED : STATUS_AMBER}>
          <rect x="22" y="2" width="4" height="5" />
          <rect x="23" y="3" width="2" height="2" fill={BOARD} />
          <rect x="23" y="6" width="2" height="1" fill={BOARD} />
        </g>
      )}

      {reviewing && (
        <g fill={STATUS_BLUE}>
          <rect x="21" y="2" width="4" height="4" />
          <rect x="24" y="6" width="2" height="2" />
          <rect x="22" y="3" width="2" height="2" fill={BOARD} />
        </g>
      )}

      <g transform={`translate(0 ${bodyLift})`}>
        <rect x="10" y="21" width="3" height="4" fill={APRON} />
        <rect x="15" y="21" width="3" height="4" fill={APRON} />
        <rect x="9" y="24" width="5" height="2" fill={BOOT} />
        <rect x="14" y="24" width="5" height="2" fill={BOOT} />

        {raisedArms ? (
          <g>
            <rect x="5" y="12" width="3" height="6" fill={SHIRT_DARK} />
            <rect x="4" y="10" width="3" height="3" fill={SKIN} />
            <rect x="20" y="12" width="3" height="6" fill={SHIRT_DARK} />
            <rect x="21" y="10" width="3" height="3" fill={SKIN} />
          </g>
        ) : talking || focused || reviewing ? (
          <g>
            <rect x="6" y="16" width="3" height="5" fill={SHIRT_DARK} />
            <rect x={focused ? 4 : 5} y={focused ? 14 : 15} width="3" height="2" fill={SKIN} />
            <rect x="19" y="15" width="3" height="6" fill={SHIRT_DARK} />
            <rect x="21" y={reviewing ? 13 : 14} width="2" height="3" fill={SKIN} />
          </g>
        ) : (
          <g>
            <rect x="6" y="16" width="3" height="5" fill={SHIRT_DARK} />
            <rect x="6" y="20" width="3" height="2" fill={SKIN} />
            <rect x="19" y="16" width="3" height="5" fill={SHIRT_DARK} />
            <rect x="19" y="20" width="3" height="2" fill={SKIN} />
          </g>
        )}

        <rect x="9" y="14" width="10" height="8" fill={SHIRT} />
        <rect x="10" y="17" width="8" height="5" fill={APRON} />
        <rect x="11" y="15" width="2" height="3" fill={APRON_LIGHT} />
        <rect x="15" y="15" width="2" height="3" fill={APRON_LIGHT} />
        <rect x="13" y="18" width="2" height="1" fill={BOARD} opacity="0.9" />

        <rect x="8" y="5" width="12" height="9" fill={SKIN} />
        <rect x="8" y="12" width="12" height="2" fill={SKIN_SHADE} opacity="0.35" />
        <rect x="7" y="4" width="14" height="4" fill={HAIR} />
        <rect x="8" y="3" width="10" height="2" fill={HAIR} />
        <rect x="18" y="5" width="3" height="5" fill={HAIR} />
        <rect x="7" y="6" width="2" height="4" fill={HAIR} />
        <rect x="10" y="4" width="3" height="1" fill={HAIR_LIGHT} />
        <rect x="12" y="14" width="4" height="1" fill={SKIN_SHADE} />

        {happy && (
          <g>
            <rect x="9" y="11" width="2" height="1" fill={BLUSH} />
            <rect x="17" y="11" width="2" height="1" fill={BLUSH} />
          </g>
        )}

        {happy ? (
          <g fill={EYE}>
            <rect x="10" y="9" width="3" height="1" />
            <rect x="15" y="9" width="3" height="1" />
          </g>
        ) : focused ? (
          <g fill={EYE}>
            <rect x="10" y="8" width="2" height="2" />
            <rect x="16" y="8" width="2" height="2" />
            <rect x="12" y="8" width="1" height="1" opacity="0.6" />
            <rect x="18" y="8" width="1" height="1" opacity="0.6" />
          </g>
        ) : (
          <g className={styles.eyes} fill={EYE}>
            <rect x="10" y="8" width="2" height="2" />
            <rect x="16" y="8" width="2" height="2" />
          </g>
        )}

        <rect x={happy ? 12 : 13} y="12" width={happy ? 4 : 2} height="1" fill={MOUTH} />
      </g>

      {celebrate && (
        <g fill={SPARKLE}>
          <rect className={styles.sparkle} x="4" y="5" width="1" height="3" />
          <rect className={styles.sparkle} x="3" y="6" width="3" height="1" />
          <rect className={styles.sparkle} x="23" y="4" width="1" height="3" />
          <rect className={styles.sparkle} x="22" y="5" width="3" height="1" />
          <rect className={styles.sparkle} x="23" y="18" width="1" height="2" />
          <rect className={styles.sparkle} x="22" y="19" width="3" height="1" />
        </g>
      )}
    </svg>
  );
}
