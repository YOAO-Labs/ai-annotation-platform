const PARAMS_BYTES = 48;

export interface RasterMaskWebGpuSeparableByteEstimate {
  sourcePackedBytes: number;
  xorPackedBytes: number;
  intermediateLogicalBytes: number;
  sourceCapacityBytes: number;
  xorCapacityBytes: number;
  readbackCapacityBytes: number;
  intermediateCapacityBytes: number;
  allocatedCapacityBytes: number;
  requiredBytes: number;
}

export interface RasterMaskWebGpuSeparableRunOptions {
  sourceWords: Uint32Array;
  sourceWordsPerRow: number;
  inputWidth: number;
  inputHeight: number;
  coreOffsetX: number;
  coreOffsetY: number;
  coreWidth: number;
  coreHeight: number;
  radius: number;
  budgetBytes: number;
}

export interface RasterMaskWebGpuSeparableSnapshot {
  state: "idle" | "ready" | "unavailable" | "closed";
  allocatedBytes: number;
  sourceCapacityBytes: number;
  xorCapacityBytes: number;
  readbackCapacityBytes: number;
  intermediateCapacityBytes: number;
  failure: string | null;
}

export interface RasterMaskWebGpuSeparableRunResult {
  xorWords: Uint32Array;
  xorWordsPerRow: number;
  metrics: {
    totalMs: number;
    uploadSubmitMs: number;
    readbackMs: number;
  };
  snapshot: RasterMaskWebGpuSeparableSnapshot;
}

interface Buffers {
  source: GPUBuffer;
  intermediate: GPUBuffer;
  xorTarget: GPUBuffer;
  readback: GPUBuffer;
  params: GPUBuffer;
  horizontalBindGroup: GPUBindGroup;
  verticalBindGroup: GPUBindGroup;
  sourceCapacityBytes: number;
  intermediateCapacityBytes: number;
  xorCapacityBytes: number;
  readbackCapacityBytes: number;
}

interface Resources {
  device: GPUDevice;
  horizontalPipeline: GPUComputePipeline;
  verticalPipeline: GPUComputePipeline;
  buffers: Buffers | null;
}

function checkedPackedBytes(width: number, height: number, label: string): number {
  if (!Number.isSafeInteger(width) || width <= 0 || !Number.isSafeInteger(height) || height <= 0) {
    throw new Error(`WebGPU separable qualification ${label} dimensions must be positive integers`);
  }
  const bytes = Math.ceil(width / 32) * height * 4;
  if (!Number.isSafeInteger(bytes) || bytes <= 0) {
    throw new Error(`WebGPU separable qualification ${label} exceeds the safe byte limit`);
  }
  return bytes;
}

function nextBufferCapacity(requiredBytes: number): number {
  let capacity = 4;
  while (capacity < requiredBytes) {
    capacity *= 2;
    if (!Number.isSafeInteger(capacity)) {
      throw new Error("WebGPU separable qualification buffer exceeds the safe byte limit");
    }
  }
  return capacity;
}

export function estimateRasterMaskWebGpuSeparableBytes(
  inputWidth: number,
  inputHeight: number,
  coreWidth = inputWidth,
  coreHeight = inputHeight,
): RasterMaskWebGpuSeparableByteEstimate {
  const sourcePackedBytes = checkedPackedBytes(inputWidth, inputHeight, "input");
  const xorPackedBytes = checkedPackedBytes(coreWidth, coreHeight, "core");
  const intermediateLogicalBytes = checkedPackedBytes(coreWidth, inputHeight, "intermediate");
  const sourceCapacityBytes = nextBufferCapacity(sourcePackedBytes);
  const xorCapacityBytes = nextBufferCapacity(xorPackedBytes);
  const readbackCapacityBytes = nextBufferCapacity(xorPackedBytes);
  const intermediateCapacityBytes = nextBufferCapacity(intermediateLogicalBytes);
  const allocatedCapacityBytes =
    sourceCapacityBytes +
    xorCapacityBytes +
    readbackCapacityBytes +
    intermediateCapacityBytes +
    PARAMS_BYTES;
  const requiredBytes =
    sourcePackedBytes + xorPackedBytes + intermediateLogicalBytes + allocatedCapacityBytes;
  if (!Number.isSafeInteger(requiredBytes)) {
    throw new Error("WebGPU separable qualification bytes exceed the safe byte limit");
  }
  return {
    sourcePackedBytes,
    xorPackedBytes,
    intermediateLogicalBytes,
    sourceCapacityBytes,
    xorCapacityBytes,
    readbackCapacityBytes,
    intermediateCapacityBytes,
    allocatedCapacityBytes,
    requiredBytes,
  };
}

const SHARED_PARAMS_AND_SOURCE = `
struct Params {
  input_width: u32,
  input_height: u32,
  source_words_per_row: u32,
  core_offset_x: u32,
  core_offset_y: u32,
  core_width: u32,
  core_height: u32,
  xor_words_per_row: u32,
  radius: u32,
  _padding_0: u32,
  _padding_1: u32,
  _padding_2: u32,
}

fn source_word(y: i32, word_x: i32) -> u32 {
  if (y < 0 || y >= i32(params.input_height) || word_x < 0 || word_x >= i32(params.source_words_per_row)) {
    return 0u;
  }
  return source[u32(y) * params.source_words_per_row + u32(word_x)];
}

fn aligned_source_word(y: i32, bit_origin: i32) -> u32 {
  if (bit_origin <= -32) {
    return 0u;
  }
  if (bit_origin < 0) {
    return source_word(y, 0) << u32(-bit_origin);
  }
  let word_x = bit_origin / 32;
  let shift = u32(bit_origin % 32);
  let lower = source_word(y, word_x);
  if (shift == 0u) {
    return lower;
  }
  return (lower >> shift) | (source_word(y, word_x + 1) << (32u - shift));
}
`;

const HORIZONTAL_SHADER = `
@group(0) @binding(0) var<storage, read> source: array<u32>;
@group(0) @binding(1) var<storage, read_write> intermediate: array<u32>;
@group(0) @binding(2) var<uniform> params: Params;

${SHARED_PARAMS_AND_SOURCE}

@compute @workgroup_size(16, 16)
fn main(@builtin(global_invocation_id) id: vec3<u32>) {
  if (id.x >= params.xor_words_per_row || id.y >= params.input_height) {
    return;
  }
  let bit_origin = i32(params.core_offset_x + id.x * 32u);
  let center = aligned_source_word(i32(id.y), bit_origin);
  let previous = aligned_source_word(i32(id.y), bit_origin - 32);
  let next = aligned_source_word(i32(id.y), bit_origin + 32);
  var expanded = center;
  var shift = 1u;
  loop {
    if (shift > params.radius) {
      break;
    }
    expanded |= center << shift;
    expanded |= center >> shift;
    expanded |= previous >> (32u - shift);
    expanded |= next << (32u - shift);
    shift += 1u;
  }
  let remaining = params.core_width - id.x * 32u;
  if (remaining < 32u) {
    expanded &= (1u << remaining) - 1u;
  }
  intermediate[id.y * params.xor_words_per_row + id.x] = expanded;
}
`;

const VERTICAL_SHADER = `
@group(0) @binding(0) var<storage, read> source: array<u32>;
@group(0) @binding(1) var<storage, read> intermediate: array<u32>;
@group(0) @binding(2) var<storage, read_write> xor_words: array<u32>;
@group(0) @binding(3) var<uniform> params: Params;

${SHARED_PARAMS_AND_SOURCE}

fn horizontal_word(y: i32, word_x: u32) -> u32 {
  if (y < 0 || y >= i32(params.input_height)) {
    return 0u;
  }
  return intermediate[u32(y) * params.xor_words_per_row + word_x];
}

@compute @workgroup_size(16, 16)
fn main(@builtin(global_invocation_id) id: vec3<u32>) {
  if (id.x >= params.xor_words_per_row || id.y >= params.core_height) {
    return;
  }
  let source_y = i32(params.core_offset_y + id.y);
  var dilated = 0u;
  var dy = -i32(params.radius);
  loop {
    if (dy > i32(params.radius)) {
      break;
    }
    dilated |= horizontal_word(source_y + dy, id.x);
    dy += 1;
  }
  let bit_origin = i32(params.core_offset_x + id.x * 32u);
  var xor_word = dilated ^ aligned_source_word(source_y, bit_origin);
  let remaining = params.core_width - id.x * 32u;
  if (remaining < 32u) {
    xor_word &= (1u << remaining) - 1u;
  }
  xor_words[id.y * params.xor_words_per_row + id.x] = xor_word;
}
`;

export class RasterMaskWebGpuSeparableQualificationProvider {
  private state: RasterMaskWebGpuSeparableSnapshot["state"] = "idle";
  private resources: Resources | null = null;
  private failure: string | null = null;

  constructor(
    private readonly gpu: GPU | null = RasterMaskWebGpuSeparableQualificationProvider.gpu(),
  ) {}

  private static gpu(): GPU | null {
    if (!globalThis.isSecureContext) return null;
    return (globalThis.navigator as (Navigator & { gpu?: GPU }) | undefined)?.gpu ?? null;
  }

  snapshot(): RasterMaskWebGpuSeparableSnapshot {
    const buffers = this.resources?.buffers;
    const sourceCapacityBytes = buffers?.sourceCapacityBytes ?? 0;
    const xorCapacityBytes = buffers?.xorCapacityBytes ?? 0;
    const readbackCapacityBytes = buffers?.readbackCapacityBytes ?? 0;
    const intermediateCapacityBytes = buffers?.intermediateCapacityBytes ?? 0;
    return {
      state: this.state,
      allocatedBytes:
        sourceCapacityBytes +
        xorCapacityBytes +
        readbackCapacityBytes +
        intermediateCapacityBytes +
        (buffers ? PARAMS_BYTES : 0),
      sourceCapacityBytes,
      xorCapacityBytes,
      readbackCapacityBytes,
      intermediateCapacityBytes,
      failure: this.failure,
    };
  }

  async initialize(): Promise<void> {
    if (this.state === "ready") return;
    if (this.state === "closed") throw new Error("WebGPU separable provider is closed");
    if (!this.gpu) return this.fail("navigator.gpu unavailable");
    let pendingDevice: GPUDevice | null = null;
    try {
      const adapter = await this.gpu.requestAdapter({ powerPreference: "high-performance" });
      if (!adapter) return this.fail("adapter unavailable");
      const device = await adapter.requestDevice();
      pendingDevice = device;
      const horizontalModule = device.createShaderModule({
        label: "Raster Mask separable horizontal qualification",
        code: HORIZONTAL_SHADER,
      });
      const verticalModule = device.createShaderModule({
        label: "Raster Mask separable vertical qualification",
        code: VERTICAL_SHADER,
      });
      const compilations = await Promise.all([
        horizontalModule.getCompilationInfo(),
        verticalModule.getCompilationInfo(),
      ]);
      const errors = compilations.flatMap((entry) =>
        entry.messages.filter((message) => message.type === "error"),
      );
      if (errors.length > 0) {
        device.destroy();
        return this.fail(errors.map((entry) => entry.message).join("; "));
      }
      const [horizontalPipeline, verticalPipeline] = await Promise.all([
        device.createComputePipelineAsync({
          label: "Raster Mask separable horizontal qualification pipeline",
          layout: "auto",
          compute: { module: horizontalModule, entryPoint: "main" },
        }),
        device.createComputePipelineAsync({
          label: "Raster Mask separable vertical qualification pipeline",
          layout: "auto",
          compute: { module: verticalModule, entryPoint: "main" },
        }),
      ]);
      this.resources = { device, horizontalPipeline, verticalPipeline, buffers: null };
      pendingDevice = null;
      this.state = "ready";
      this.failure = null;
    } catch (error) {
      pendingDevice?.destroy();
      this.destroyResources();
      this.fail(error instanceof Error ? error.message : "initialization failed");
    }
  }

  async run(
    options: RasterMaskWebGpuSeparableRunOptions,
  ): Promise<RasterMaskWebGpuSeparableRunResult> {
    if (this.state !== "ready" || !this.resources) {
      throw new Error(this.failure ?? "WebGPU separable provider is not ready");
    }
    this.validateOptions(options);
    const totalStarted = performance.now();
    const xorWordsPerRow = Math.ceil(options.coreWidth / 32);
    const sourceBytes = options.sourceWords.byteLength;
    const xorBytes = xorWordsPerRow * options.coreHeight * 4;
    const intermediateBytes = xorWordsPerRow * options.inputHeight * 4;
    const buffers = this.ensureBuffers(
      sourceBytes,
      xorBytes,
      intermediateBytes,
      options.budgetBytes,
    );
    const uploadStarted = performance.now();
    const { device, horizontalPipeline, verticalPipeline } = this.resources;
    const dispatchX = Math.ceil(xorWordsPerRow / 16);
    const horizontalDispatchY = Math.ceil(options.inputHeight / 16);
    const verticalDispatchY = Math.ceil(options.coreHeight / 16);
    const maxDispatch = Number(device.limits.maxComputeWorkgroupsPerDimension);
    if (Math.max(dispatchX, horizontalDispatchY, verticalDispatchY) > maxDispatch) {
      throw new Error("WebGPU separable qualification exceeds device dispatch limits");
    }
    device.queue.writeBuffer(buffers.source, 0, options.sourceWords);
    device.queue.writeBuffer(
      buffers.params,
      0,
      new Uint32Array([
        options.inputWidth,
        options.inputHeight,
        options.sourceWordsPerRow,
        options.coreOffsetX,
        options.coreOffsetY,
        options.coreWidth,
        options.coreHeight,
        xorWordsPerRow,
        options.radius,
        0,
        0,
        0,
      ]),
    );
    const encoder = device.createCommandEncoder({
      label: "Raster Mask separable qualification encoder",
    });
    const horizontalPass = encoder.beginComputePass({
      label: "Raster Mask separable horizontal qualification pass",
    });
    horizontalPass.setPipeline(horizontalPipeline);
    horizontalPass.setBindGroup(0, buffers.horizontalBindGroup);
    horizontalPass.dispatchWorkgroups(dispatchX, horizontalDispatchY);
    horizontalPass.end();
    const verticalPass = encoder.beginComputePass({
      label: "Raster Mask separable vertical qualification pass",
    });
    verticalPass.setPipeline(verticalPipeline);
    verticalPass.setBindGroup(0, buffers.verticalBindGroup);
    verticalPass.dispatchWorkgroups(dispatchX, verticalDispatchY);
    verticalPass.end();
    encoder.copyBufferToBuffer(buffers.xorTarget, 0, buffers.readback, 0, xorBytes);
    device.queue.submit([encoder.finish()]);
    const uploadSubmitMs = performance.now() - uploadStarted;
    const readbackStarted = performance.now();
    await buffers.readback.mapAsync(GPUMapMode.READ, 0, xorBytes);
    let xorWords: Uint32Array;
    try {
      xorWords = new Uint32Array(buffers.readback.getMappedRange(0, xorBytes).slice(0));
    } finally {
      buffers.readback.unmap();
    }
    this.validateTail(xorWords, xorWordsPerRow, options.coreWidth, options.coreHeight);
    return {
      xorWords,
      xorWordsPerRow,
      metrics: {
        totalMs: performance.now() - totalStarted,
        uploadSubmitMs,
        readbackMs: performance.now() - readbackStarted,
      },
      snapshot: this.snapshot(),
    };
  }

  dispose(): void {
    if (this.state === "closed") return;
    this.destroyResources();
    this.state = "closed";
  }

  private validateOptions(options: RasterMaskWebGpuSeparableRunOptions): void {
    const sourceWordsPerRow = Math.ceil(options.inputWidth / 32);
    if (
      !(options.sourceWords instanceof Uint32Array) ||
      options.sourceWordsPerRow !== sourceWordsPerRow ||
      options.sourceWords.length !== sourceWordsPerRow * options.inputHeight ||
      !Number.isSafeInteger(options.coreOffsetX) ||
      !Number.isSafeInteger(options.coreOffsetY) ||
      options.coreOffsetX < 0 ||
      options.coreOffsetY < 0 ||
      options.coreOffsetX + options.coreWidth > options.inputWidth ||
      options.coreOffsetY + options.coreHeight > options.inputHeight ||
      !Number.isInteger(options.radius) ||
      options.radius < 1 ||
      options.radius > 31 ||
      !Number.isSafeInteger(options.budgetBytes) ||
      options.budgetBytes <= 0
    ) {
      throw new Error("WebGPU separable qualification input is invalid");
    }
    estimateRasterMaskWebGpuSeparableBytes(
      options.inputWidth,
      options.inputHeight,
      options.coreWidth,
      options.coreHeight,
    );
  }

  private ensureBuffers(
    sourceBytes: number,
    xorBytes: number,
    intermediateBytes: number,
    budgetBytes: number,
  ): Buffers {
    const resources = this.resources!;
    const current = resources.buffers;
    const sourceCapacityBytes = Math.max(
      current?.sourceCapacityBytes ?? 0,
      nextBufferCapacity(sourceBytes),
    );
    const xorCapacityBytes = Math.max(current?.xorCapacityBytes ?? 0, nextBufferCapacity(xorBytes));
    const readbackCapacityBytes = Math.max(
      current?.readbackCapacityBytes ?? 0,
      nextBufferCapacity(xorBytes),
    );
    const intermediateCapacityBytes = Math.max(
      current?.intermediateCapacityBytes ?? 0,
      nextBufferCapacity(intermediateBytes),
    );
    const allocatedBytes =
      sourceCapacityBytes +
      xorCapacityBytes +
      readbackCapacityBytes +
      intermediateCapacityBytes +
      PARAMS_BYTES;
    if (allocatedBytes > budgetBytes) {
      throw new Error("WebGPU separable qualification budget is insufficient");
    }
    const maxBufferSize = Number(resources.device.limits.maxBufferSize);
    const maxStorageSize = Number(resources.device.limits.maxStorageBufferBindingSize);
    if (
      Math.max(
        sourceCapacityBytes,
        xorCapacityBytes,
        readbackCapacityBytes,
        intermediateCapacityBytes,
      ) > maxBufferSize ||
      Math.max(sourceCapacityBytes, xorCapacityBytes, intermediateCapacityBytes) > maxStorageSize
    ) {
      throw new Error("WebGPU separable qualification exceeds device buffer limits");
    }
    if (
      current &&
      current.sourceCapacityBytes >= sourceBytes &&
      current.xorCapacityBytes >= xorBytes &&
      current.readbackCapacityBytes >= xorBytes &&
      current.intermediateCapacityBytes >= intermediateBytes
    ) {
      return current;
    }

    resources.buffers = null;
    this.destroyBuffers(current);
    const { device, horizontalPipeline, verticalPipeline } = resources;
    const created: GPUBuffer[] = [];
    try {
      const create = (label: string, size: number, usage: GPUBufferUsageFlags) => {
        const buffer = device.createBuffer({ label, size, usage });
        created.push(buffer);
        return buffer;
      };
      const source = create(
        "Raster Mask separable qualification source",
        sourceCapacityBytes,
        GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_DST,
      );
      const intermediate = create(
        "Raster Mask separable qualification intermediate",
        intermediateCapacityBytes,
        GPUBufferUsage.STORAGE,
      );
      const xorTarget = create(
        "Raster Mask separable qualification XOR target",
        xorCapacityBytes,
        GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_SRC,
      );
      const readback = create(
        "Raster Mask separable qualification readback",
        readbackCapacityBytes,
        GPUBufferUsage.COPY_DST | GPUBufferUsage.MAP_READ,
      );
      const params = create(
        "Raster Mask separable qualification params",
        PARAMS_BYTES,
        GPUBufferUsage.UNIFORM | GPUBufferUsage.COPY_DST,
      );
      const horizontalBindGroup = device.createBindGroup({
        label: "Raster Mask separable qualification horizontal bind group",
        layout: horizontalPipeline.getBindGroupLayout(0),
        entries: [
          { binding: 0, resource: { buffer: source } },
          { binding: 1, resource: { buffer: intermediate } },
          { binding: 2, resource: { buffer: params } },
        ],
      });
      const verticalBindGroup = device.createBindGroup({
        label: "Raster Mask separable qualification vertical bind group",
        layout: verticalPipeline.getBindGroupLayout(0),
        entries: [
          { binding: 0, resource: { buffer: source } },
          { binding: 1, resource: { buffer: intermediate } },
          { binding: 2, resource: { buffer: xorTarget } },
          { binding: 3, resource: { buffer: params } },
        ],
      });
      const buffers = {
        source,
        intermediate,
        xorTarget,
        readback,
        params,
        horizontalBindGroup,
        verticalBindGroup,
        sourceCapacityBytes,
        intermediateCapacityBytes,
        xorCapacityBytes,
        readbackCapacityBytes,
      };
      resources.buffers = buffers;
      return buffers;
    } catch (error) {
      for (const buffer of created) buffer.destroy();
      throw error;
    }
  }

  private validateTail(
    xorWords: Uint32Array,
    wordsPerRow: number,
    width: number,
    height: number,
  ): void {
    if (xorWords.length !== wordsPerRow * height) {
      throw new Error("WebGPU separable qualification readback length is invalid");
    }
    const remaining = width % 32;
    if (remaining === 0) return;
    const validMask = 0xffff_ffff >>> (32 - remaining);
    for (let y = 0; y < height; y += 1) {
      if ((xorWords[y * wordsPerRow + wordsPerRow - 1]! & ~validMask) !== 0) {
        throw new Error("WebGPU separable qualification readback tail is invalid");
      }
    }
  }

  private fail(message: string): void {
    this.failure = message;
    this.state = "unavailable";
  }

  private destroyBuffers(buffers: Buffers | null | undefined): void {
    if (!buffers) return;
    buffers.source.destroy();
    buffers.intermediate.destroy();
    buffers.xorTarget.destroy();
    buffers.readback.destroy();
    buffers.params.destroy();
  }

  private destroyResources(): void {
    const resources = this.resources;
    this.resources = null;
    if (!resources) return;
    this.destroyBuffers(resources.buffers);
    resources.device.destroy();
  }
}
