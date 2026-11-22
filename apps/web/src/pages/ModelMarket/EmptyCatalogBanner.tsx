import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";

interface Props {
  taskCount: number;
  onGoToRegistry: () => void;
}

export function EmptyCatalogBanner({ taskCount, onGoToRegistry }: Props) {
  return (
    <div className="mb-3 flex items-center gap-3 rounded-md border border-dashed border-border bg-brand/10 px-4 py-3.5">
      <div className="flex flex-[0_0_32px] items-center justify-center text-brand">
        <Icon name="layers" size={24} />
      </div>
      <div className="flex flex-auto flex-col gap-0.5">
        <div className="text-sm font-semibold text-foreground">
          平台支持 {taskCount} 类 AI 标注能力，当前列表暂无模型。
        </div>
        <div className="text-xs leading-normal text-muted-foreground">
          下方是协议支持的能力，参考服务不代表已注册或已连接。注册后还需检查连接，
          并在需要使用的项目中启用服务。
        </div>
      </div>
      <div className="flex flex-[0_0_auto] gap-2">
        <Button size="sm" onClick={onGoToRegistry}>
          <Icon name="plus" size={11} /> 查看注册管理
        </Button>
      </div>
    </div>
  );
}
