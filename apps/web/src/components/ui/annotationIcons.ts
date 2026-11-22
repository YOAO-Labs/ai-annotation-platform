import { createLucideIcon } from "lucide-react";

export const RotatedBox = createLucideIcon("AnnotationRotatedBox", [
  ["path", { d: "m4 10 12-4 4 12-12 4Z", key: "box" }],
  ["path", { d: "m10 8-1-3", key: "stem" }],
  ["circle", { cx: "8.5", cy: "3.5", r: "1.5", key: "handle" }],
]);

export const Keypoints = createLucideIcon("AnnotationKeypoints", [
  ["path", { d: "m6 8 6 4 6-6m-6 6-5 7m5-7 7 6", key: "bones" }],
  ["circle", { cx: "6", cy: "8", r: "2", key: "left" }],
  ["circle", { cx: "18", cy: "6", r: "2", key: "right" }],
  ["circle", { cx: "7", cy: "19", r: "2", key: "bottom-left" }],
  ["circle", { cx: "19", cy: "18", r: "2", key: "bottom-right" }],
]);

export const AnnotationPolygon = createLucideIcon("AnnotationPolygon", [
  ["path", { d: "m4 9 6-6 10 4-3 13-12-2Z", key: "outline" }],
  ["circle", { cx: "4", cy: "9", r: "1.5", key: "vertex" }],
]);

export const AnnotationPolyline = createLucideIcon("AnnotationPolyline", [
  ["path", { d: "m4 18 5-12 6 11 5-12", key: "line" }],
  ["circle", { cx: "4", cy: "18", r: "1.5", key: "start" }],
  ["circle", { cx: "20", cy: "5", r: "1.5", key: "end" }],
]);

export const PolygonTrack = createLucideIcon("AnnotationPolygonTrack", [
  ["path", { d: "M3 8v13h13", key: "frame" }],
  ["path", { d: "m8 8 5-5 8 4-3 10-9-2Z", key: "polygon" }],
]);

export const PolylineTrack = createLucideIcon("AnnotationPolylineTrack", [
  ["path", { d: "M3 8v13h13", key: "frame" }],
  ["path", { d: "m8 15 4-11 5 11 4-10", key: "line" }],
]);

export const MaskTrack = createLucideIcon("AnnotationMaskTrack", [
  ["path", { d: "M3 8v13h13", key: "frame" }],
  [
    "path",
    { d: "M8 6c0-4 6-4 7-1 5-2 8 3 5 6 2 4-2 7-5 5-4 3-9 0-7-4-3-2-2-5 0-6Z", key: "region" },
  ],
]);

export const SmartScribble = createLucideIcon("AnnotationSmartScribble", [
  ["path", { d: "M4 6c-4 5 9 2 7 7S1 13 3 18s10 3 13-1", key: "scribble" }],
  ["path", { d: "m17 2 1.5 3.5L22 7l-3.5 1.5L17 12l-1.5-3.5L12 7l3.5-1.5Z", key: "spark" }],
]);
