import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import type { SensorCalibration, SensorCalibrationHistoryOut } from "@/api/generated";
import { copyToClipboard } from "@/lib/clipboard";

vi.mock("@/lib/clipboard", () => ({ copyToClipboard: vi.fn().mockResolvedValue(true) }));

const calibrationHookMock = vi.hoisted(() => vi.fn());
const toastPushMock = vi.hoisted(() => vi.fn());

vi.mock("@/hooks/useSensorCalibration", () => ({
  useSensorCalibration: calibrationHookMock,
}));

vi.mock("@/components/ui/Toast", () => ({
  useToastStore: (selector: (state: { push: typeof toastPushMock }) => unknown) =>
    selector({ push: toastPushMock }),
}));

import { SensorCalibrationSheet } from "./SensorCalibrationSheet";

const BASELINE: SensorCalibration = {
  intrinsic: [100, 0, 50, 0, 100, 40, 0, 0, 1],
  extrinsic: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
  rect: null,
};
const CURRENT: SensorCalibration = {
  ...BASELINE,
  intrinsic: [110, 0, 50, 0, 100, 40, 0, 0, 1],
};
const HISTORY: SensorCalibrationHistoryOut = {
  current_revision: 2,
  current_digest: "b".repeat(64),
  items: [
    {
      dataset_item_id: "camera-1",
      revision: 2,
      digest: "b".repeat(64),
      calibration: CURRENT,
      created_at: "2026-08-27T08:00:00Z",
    },
    {
      dataset_item_id: "camera-1",
      revision: 1,
      digest: "a".repeat(64),
      calibration: BASELINE,
      created_at: null,
    },
  ],
};

function renderSheet(canManage = true) {
  return render(
    <SensorCalibrationSheet
      open
      onOpenChange={vi.fn()}
      taskId="task-1"
      projectId="project-1"
      cameraName="CAM_FRONT"
      cameraRole="camera_front"
      calibration={CURRENT}
      revision={2}
      digest={"b".repeat(64)}
      canManage={canManage}
    />,
  );
}

describe("SensorCalibrationSheet", () => {
  const mutateAsync = vi.fn();
  const refetch = vi.fn();
  const refreshRelated = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    mutateAsync.mockResolvedValue({
      dataset_item_id: "camera-1",
      revision: 3,
      digest: "c".repeat(64),
      calibration: { ...CURRENT, intrinsic: [120, 0, 50, 0, 100, 40, 0, 0, 1] },
      created_at: "2026-08-27T09:00:00Z",
    });
    refreshRelated.mockResolvedValue(true);
    calibrationHookMock.mockReturnValue({
      query: {
        data: HISTORY,
        isLoading: false,
        isFetching: false,
        isError: false,
        error: null,
        refetch,
      },
      update: { mutateAsync, isPending: false },
      refreshRelated,
    });
  });

  it("非管理者可查看当前矩阵和历史，但不能编辑", () => {
    renderSheet(false);
    expect(screen.getByText("只读标定")).toBeTruthy();
    expect(screen.getByText("Revision 历史")).toBeTruthy();
    expect(screen.getByText("虚拟基线")).toBeTruthy();
    expect(screen.queryByText("编辑当前标定")).toBeNull();
    expect(screen.queryByText("加载为草稿")).toBeNull();
    expect(screen.getByRole("button", { name: "关闭" })).toBeTruthy();
    expect(document.querySelector('[data-slot="sheet-footer"]')).toBeNull();
  });

  it("矩阵值按原始数值精度展示", () => {
    const preciseValue = 0.12345678912345678;
    calibrationHookMock.mockReturnValue({
      query: {
        data: {
          ...HISTORY,
          items: [
            {
              ...HISTORY.items[0],
              calibration: {
                ...CURRENT,
                intrinsic: [preciseValue, 0, 50, 0, 100, 40, 0, 0, 1],
              },
            },
          ],
        },
        isLoading: false,
        isFetching: false,
        isError: false,
        error: null,
        refetch,
      },
      update: { mutateAsync, isPending: false },
      refreshRelated,
    });

    renderSheet(false);

    expect(screen.getByText(String(preciseValue))).toBeTruthy();
  });

  it("当前和历史 revision 可复制完整哈希，不修改标定", async () => {
    renderSheet(false);
    expect(screen.queryByText(HISTORY.current_digest)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "复制当前标定哈希" }));
    await waitFor(() => expect(copyToClipboard).toHaveBeenCalledWith(HISTORY.current_digest));
    fireEvent.click(screen.getByRole("button", { name: "复制 Revision 1 标定哈希" }));
    await waitFor(() => expect(copyToClipboard).toHaveBeenCalledWith(HISTORY.items[1].digest));
    expect(mutateAsync).not.toHaveBeenCalled();
    expect(screen.getByText("虚拟基线")).toBeInTheDocument();
  });

  it("无效 JSON 不发请求，有效变更基于当前 revision 追加", async () => {
    renderSheet();
    fireEvent.click(screen.getByText("编辑当前标定"));
    const textarea = screen.getByLabelText("标定 JSON");
    fireEvent.change(textarea, { target: { value: "{" } });
    expect(screen.getByText("JSON 语法不正确")).toBeTruthy();
    expect(screen.getByText("追加 revision")).toBeDisabled();
    expect(mutateAsync).not.toHaveBeenCalled();

    const next = { ...CURRENT, intrinsic: [120, 0, 50, 0, 100, 40, 0, 0, 1] };
    fireEvent.change(textarea, { target: { value: JSON.stringify(next) } });
    fireEvent.click(screen.getByText("追加 revision"));
    await waitFor(() =>
      expect(mutateAsync).toHaveBeenCalledWith({
        payload: {
          calibration: next,
          expected_revision: 2,
          expected_digest: "b".repeat(64),
        },
        idempotencyKey: expect.stringMatching(/^[0-9a-f-]{36}$/),
      }),
    );
    expect(toastPushMock).toHaveBeenCalledWith(
      expect.objectContaining({ msg: "标定已追加为 revision 3", kind: "success" }),
    );
  });

  it("历史快照作为草稿基于最新 revision 提交", async () => {
    renderSheet();
    fireEvent.click(screen.getByText("加载为草稿"));
    expect(screen.getByLabelText("标定 JSON")).toHaveValue(JSON.stringify(BASELINE, null, 2));
    fireEvent.click(screen.getByText("追加 revision"));
    await waitFor(() =>
      expect(mutateAsync).toHaveBeenCalledWith({
        payload: {
          calibration: BASELINE,
          expected_revision: 2,
          expected_digest: "b".repeat(64),
        },
        idempotencyKey: expect.stringMatching(/^[0-9a-f-]{36}$/),
      }),
    );
  });

  it("并发冲突后保留草稿但不推进写入凭据", async () => {
    mutateAsync.mockRejectedValueOnce(new ApiError(409, "camera calibration changed"));
    renderSheet();
    fireEvent.click(screen.getByText("编辑当前标定"));
    const textarea = screen.getByLabelText("标定 JSON");
    const next = { ...CURRENT, intrinsic: [130, 0, 50, 0, 100, 40, 0, 0, 1] };
    const source = JSON.stringify(next);
    fireEvent.change(textarea, { target: { value: source } });
    fireEvent.click(screen.getByText("追加 revision"));

    expect(await screen.findByText("检测到新的 revision")).toBeTruthy();
    expect(screen.getByText(/当前草稿已保留/)).toBeTruthy();
    expect(textarea).toHaveValue(source);
    expect(refreshRelated).toHaveBeenCalledOnce();

    expect(screen.getByText("追加 revision")).toBeDisabled();
    fireEvent.click(screen.getByText("追加 revision"));
    expect(mutateAsync).toHaveBeenCalledOnce();
  });

  it("不确定的响应重试时复用同一幂等键", async () => {
    mutateAsync.mockRejectedValueOnce(new Error("connection lost"));
    renderSheet();
    fireEvent.click(screen.getByText("编辑当前标定"));
    const textarea = screen.getByLabelText("标定 JSON");
    const next = { ...CURRENT, intrinsic: [140, 0, 50, 0, 100, 40, 0, 0, 1] };
    fireEvent.change(textarea, { target: { value: JSON.stringify(next) } });
    fireEvent.click(screen.getByText("追加 revision"));
    await screen.findByText(/connection lost/);
    const firstKey = mutateAsync.mock.calls[0][0].idempotencyKey;

    fireEvent.click(screen.getByText("追加 revision"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(2));

    expect(mutateAsync.mock.calls[1][0].idempotencyKey).toBe(firstKey);
  });

  it("后台刷新保留草稿，用户可显式基于最新版重新编辑", () => {
    const view = renderSheet();
    fireEvent.click(screen.getByText("编辑当前标定"));
    const textarea = screen.getByLabelText("标定 JSON");
    const draft = { ...CURRENT, intrinsic: [130, 0, 50, 0, 100, 40, 0, 0, 1] };
    fireEvent.change(textarea, { target: { value: JSON.stringify(draft) } });

    const latest: SensorCalibration = {
      ...CURRENT,
      extrinsic: [1, 0, 0, 1, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
    };
    calibrationHookMock.mockReturnValue({
      query: {
        data: {
          current_revision: 3,
          current_digest: "c".repeat(64),
          items: [
            {
              dataset_item_id: "camera-1",
              revision: 3,
              digest: "c".repeat(64),
              calibration: latest,
              created_at: "2026-08-27T09:00:00Z",
            },
            ...HISTORY.items,
          ],
        },
        isLoading: false,
        isFetching: false,
        isError: false,
        error: null,
        refetch,
      },
      update: { mutateAsync, isPending: false },
      refreshRelated,
    });
    view.rerender(
      <SensorCalibrationSheet
        open
        onOpenChange={vi.fn()}
        taskId="task-1"
        projectId="project-1"
        cameraName="CAM_FRONT"
        cameraRole="camera_front"
        calibration={latest}
        revision={3}
        digest={"c".repeat(64)}
        canManage
      />,
    );

    expect(textarea).toHaveValue(JSON.stringify(draft));
    expect(screen.getByText("检测到新的 revision")).toBeTruthy();
    expect(screen.getByText("追加 revision")).toBeDisabled();

    fireEvent.click(screen.getByText("基于最新 revision 重新编辑"));
    expect(textarea).toHaveValue(JSON.stringify(latest, null, 2));
    expect(screen.queryByText("检测到新的 revision")).toBeNull();
  });
});
