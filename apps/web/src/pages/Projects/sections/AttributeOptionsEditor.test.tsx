import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import {
  AttributeOptionsEditor,
  parseOptionToken,
  parseOptionLines,
  parseOptionTokens,
  serializeOptionLines,
} from "./AttributeOptionsEditor";
import type { AttributeFieldOption } from "@/api/projects";

const OPTS: AttributeFieldOption[] = [
  { value: "car", label: "小车" },
  { value: "truck", label: "卡车" },
];

describe("parseOptionToken", () => {
  it("无冒号时 value = label", () => {
    expect(parseOptionToken("car")).toEqual({ value: "car", label: "car" });
  });

  it("只在第一个冒号切分，label 可含冒号", () => {
    expect(parseOptionToken("ratio:宽:高")).toEqual({ value: "ratio", label: "宽:高" });
  });

  it("value 为空视为非法", () => {
    expect(parseOptionToken(":小车")).toBeNull();
    expect(parseOptionToken("  ")).toBeNull();
    expect(parseOptionToken("")).toBeNull();
  });

  it("label 为空时回落到 value", () => {
    expect(parseOptionToken("car:")).toEqual({ value: "car", label: "car" });
  });

  it("两侧空白被裁剪", () => {
    expect(parseOptionToken("  car : 小车 ")).toEqual({ value: "car", label: "小车" });
  });
});

describe("parseOptionLines（批量 textarea：只按换行切）", () => {
  it("label 可含逗号", () => {
    expect(parseOptionLines("truck:卡车, 大型")).toEqual([{ value: "truck", label: "卡车, 大型" }]);
  });

  it("忽略空行与非法行", () => {
    expect(parseOptionLines("car:小车\n\n:坏行\ntruck:卡车")).toEqual(OPTS);
  });

  it("重复 value 只保留首次", () => {
    expect(parseOptionLines("car:小车\ncar:轿车")).toEqual([{ value: "car", label: "小车" }]);
  });
});

describe("parseOptionTokens（新增框：换行或逗号切）", () => {
  it("逗号分隔的多个一次解析", () => {
    expect(parseOptionTokens("car:小车, truck:卡车")).toEqual(OPTS);
  });
});

describe("serializeOptionLines", () => {
  it("value === label 时只写一次", () => {
    expect(serializeOptionLines([{ value: "car", label: "car" }])).toBe("car");
  });

  it("parse ∘ serialize 幂等", () => {
    const round = parseOptionLines(serializeOptionLines(OPTS));
    expect(round).toEqual(OPTS);
  });
});

describe("<AttributeOptionsEditor /> chip 模式", () => {
  it("每个选项渲染一枚 chip，value 与 label 不同时并列展示 value", () => {
    render(<AttributeOptionsEditor value={OPTS} onChange={() => {}} />);
    expect(screen.getByTitle("编辑「小车」(value=car)")).toBeInTheDocument();
    expect(screen.getByText("car")).toBeInTheDocument();
  });

  it("value 与 label 相同时不重复展示 value", () => {
    render(<AttributeOptionsEditor value={[{ value: "car", label: "car" }]} onChange={() => {}} />);
    expect(screen.getAllByText("car")).toHaveLength(1);
  });

  it("回车新增一个选项", () => {
    const onChange = vi.fn();
    render(<AttributeOptionsEditor value={OPTS} onChange={onChange} />);
    const input = screen.getByLabelText("添加选项");
    fireEvent.change(input, { target: { value: "bus:公交" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith([...OPTS, { value: "bus", label: "公交" }]);
  });

  it("粘贴逗号分隔的多个后回车，一次全部加入", () => {
    const onChange = vi.fn();
    render(<AttributeOptionsEditor value={[]} onChange={onChange} />);
    const input = screen.getByLabelText("添加选项");
    fireEvent.change(input, { target: { value: "car:小车, truck:卡车" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith(OPTS);
  });

  it("新增已存在的 value 被忽略", () => {
    const onChange = vi.fn();
    render(<AttributeOptionsEditor value={OPTS} onChange={onChange} />);
    const input = screen.getByLabelText("添加选项");
    fireEvent.change(input, { target: { value: "car:轿车" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onChange).not.toHaveBeenCalled();
  });

  it("点 × 删除对应选项", () => {
    const onChange = vi.fn();
    render(<AttributeOptionsEditor value={OPTS} onChange={onChange} />);
    fireEvent.click(screen.getByTitle("删除「小车」"));
    expect(onChange).toHaveBeenCalledWith([{ value: "truck", label: "卡车" }]);
  });

  it("空输入框按退格删除末尾 chip", () => {
    const onChange = vi.fn();
    render(<AttributeOptionsEditor value={OPTS} onChange={onChange} />);
    fireEvent.keyDown(screen.getByLabelText("添加选项"), { key: "Backspace" });
    expect(onChange).toHaveBeenCalledWith([{ value: "car", label: "小车" }]);
  });

  it("点 chip 就地编辑，回车提交", () => {
    const onChange = vi.fn();
    render(<AttributeOptionsEditor value={OPTS} onChange={onChange} />);
    fireEvent.click(screen.getByTitle("编辑「小车」(value=car)"));
    const labelInput = screen.getByLabelText("选项显示名");
    fireEvent.change(labelInput, { target: { value: "轿车" } });
    fireEvent.keyDown(labelInput, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith([
      { value: "car", label: "轿车" },
      { value: "truck", label: "卡车" },
    ]);
  });

  it("就地编辑按 Esc 取消，不触发 onChange", () => {
    const onChange = vi.fn();
    render(<AttributeOptionsEditor value={OPTS} onChange={onChange} />);
    fireEvent.click(screen.getByTitle("编辑「小车」(value=car)"));
    fireEvent.keyDown(screen.getByLabelText("选项显示名"), { key: "Escape" });
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByTitle("编辑「小车」(value=car)")).toBeInTheDocument();
  });

  it("传入重复 value 时提示", () => {
    const dupes = [...OPTS, { value: "car", label: "轿车" }];
    render(<AttributeOptionsEditor value={dupes} onChange={() => {}} />);
    expect(screen.getByText(/value 重复：car/)).toBeInTheDocument();
  });
});

describe("<AttributeOptionsEditor /> 批量模式", () => {
  const toBulk = () => fireEvent.click(screen.getByText("批量编辑"));

  it("切入批量模式后 textarea 是一行一个", () => {
    render(<AttributeOptionsEditor value={OPTS} onChange={() => {}} />);
    toBulk();
    expect(screen.getByLabelText("批量编辑选项")).toHaveValue("car:小车\ntruck:卡车");
  });

  it("正在输入的空行不被吞掉（旧 filter(Boolean) 回归）", () => {
    render(<AttributeOptionsEditor value={OPTS} onChange={() => {}} />);
    toBulk();
    const ta = screen.getByLabelText("批量编辑选项");
    fireEvent.change(ta, { target: { value: "car:小车\ntruck:卡车\n" } });
    expect(ta).toHaveValue("car:小车\ntruck:卡车\n");
  });

  it("编辑 textarea 时上报解析后的 options", () => {
    const onChange = vi.fn();
    render(<AttributeOptionsEditor value={OPTS} onChange={onChange} />);
    toBulk();
    fireEvent.change(screen.getByLabelText("批量编辑选项"), {
      target: { value: "car:小车\ntruck:卡车\nbus:公交" },
    });
    expect(onChange).toHaveBeenCalledWith([...OPTS, { value: "bus", label: "公交" }]);
  });

  it("调整行序即调整选项顺序", () => {
    const onChange = vi.fn();
    render(<AttributeOptionsEditor value={OPTS} onChange={onChange} />);
    toBulk();
    fireEvent.change(screen.getByLabelText("批量编辑选项"), {
      target: { value: "truck:卡车\ncar:小车" },
    });
    expect(onChange).toHaveBeenCalledWith([
      { value: "truck", label: "卡车" },
      { value: "car", label: "小车" },
    ]);
  });

  it("非法行与重复行分别计数提示", () => {
    render(<AttributeOptionsEditor value={[]} onChange={() => {}} />);
    toBulk();
    fireEvent.change(screen.getByLabelText("批量编辑选项"), {
      target: { value: "car:小车\n:坏行\ncar:轿车" },
    });
    expect(screen.getByText("1 个选项")).toBeInTheDocument();
    expect(screen.getByText("1 行缺少 value，已忽略")).toBeInTheDocument();
    expect(screen.getByText("1 行 value 重复，已忽略")).toBeInTheDocument();
  });
});
