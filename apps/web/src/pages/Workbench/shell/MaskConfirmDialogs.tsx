import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/shadcn/ui/alert-dialog";

export interface MaskConfirmDialogsProps {
  videoToolConfirmation: {
    open: boolean;
    settle: (confirmed: boolean) => void;
  };
  emptyRegion: {
    open: boolean;
    close: () => void;
    confirm: () => void;
  };
  instanceDelete: {
    open: boolean;
    setOpen: (open: boolean) => void;
    count: number;
    confirm: () => void;
  };
}

export function MaskConfirmDialogs({
  videoToolConfirmation,
  emptyRegion,
  instanceDelete,
}: MaskConfirmDialogsProps) {
  return (
    <>
      <AlertDialog
        open={videoToolConfirmation.open}
        onOpenChange={(open) => {
          if (!open) videoToolConfirmation.settle(false);
        }}
      >
        <AlertDialogContent
          size="sm"
          className="z-app-drawer"
          overlayProps={{ className: "z-app-drawer-backdrop" }}
          data-workbench-video-tool-confirm
        >
          <AlertDialogHeader>
            <AlertDialogTitle>切换视频工具</AlertDialogTitle>
            <AlertDialogDescription>
              当前源帧还有未完成的绘制。继续绘制会保留原工具、范围和草稿；丢弃后再切换。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>继续绘制</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              onClick={() => videoToolConfirmation.settle(true)}
            >
              丢弃并切换
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
      <AlertDialog
        open={emptyRegion.open}
        onOpenChange={(open) => {
          if (!open) emptyRegion.close();
        }}
      >
        <AlertDialogContent
          size="sm"
          className="z-app-drawer"
          overlayProps={{ className: "z-app-drawer-backdrop" }}
        >
          <AlertDialogHeader>
            <AlertDialogTitle>确认清空当前 Mask？</AlertDialogTitle>
            <AlertDialogDescription>
              该操作会把当前对象变为空
              Mask。应用后仍可用撤销恢复，但保存时需要选择删除对象或继续编辑。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>返回预览</AlertDialogCancel>
            <AlertDialogAction variant="destructive" onClick={emptyRegion.confirm}>
              确认清空
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
      <AlertDialog open={instanceDelete.open} onOpenChange={instanceDelete.setOpen}>
        <AlertDialogContent
          size="sm"
          className="z-app-drawer"
          overlayProps={{ className: "z-app-drawer-backdrop" }}
        >
          <AlertDialogHeader>
            <AlertDialogTitle>确认删除 {instanceDelete.count} 个 Mask 实例？</AlertDialogTitle>
            <AlertDialogDescription>
              本次原子操作会删除面积为零或被替换的图片 Mask。提交后需通过审计记录追溯。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>返回预览</AlertDialogCancel>
            <AlertDialogAction variant="destructive" onClick={instanceDelete.confirm}>
              确认删除并提交
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
