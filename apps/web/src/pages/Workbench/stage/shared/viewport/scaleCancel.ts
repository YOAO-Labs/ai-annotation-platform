export function screenToWorld(px: number, scale: number): number {
  return px / scale;
}

export function dashToWorld(dash: number[], scale: number): number[] {
  return dash.map((d) => d / scale);
}
