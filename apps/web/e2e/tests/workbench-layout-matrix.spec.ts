import { registerWorkbenchLayoutMatrixTests } from "../helpers/workbench-layout-matrix";

// Image and video workbench layouts run in the chromium project: their stages
// are 2D canvases that need no WebGL, so they must not inherit the SwiftShader
// software renderer of the pointcloud project (see
// workbench-pointcloud-layout-matrix.spec.ts), which starves CI runners during
// stress reloads.

registerWorkbenchLayoutMatrixTests(["image", "video"]);
