import { ListPagination } from "@/components/ui/ListPagination";
import { useListPagination } from "@/hooks/useListPagination";
import { listStyles } from "@/components/ui/listStyles";
import { useEffect, useState } from "react";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useToastStore } from "@/components/ui/Toast";
import { useCreateGroup, useDeleteGroup, useGroups, useUpdateGroup } from "@/hooks/useGroups";
import type { GroupResponse } from "@/api/groups";
import styles from "./GroupManageModal.module.css";

interface Props {
  open: boolean;
  onClose: () => void;
}

export function GroupManageModal({ open, onClose }: Props) {
  const { data: groups = [], isLoading } = useGroups(open);
  const pagination = useListPagination(groups, String(open));
  const createMut = useCreateGroup();
  const updateMut = useUpdateGroup();
  const deleteMut = useDeleteGroup();
  const pushToast = useToastStore((s) => s.push);

  const [newName, setNewName] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingName, setEditingName] = useState("");
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);

  useEffect(() => {
    if (!open) {
      setNewName("");
      setEditingId(null);
      setPendingDelete(null);
      createMut.reset();
      updateMut.reset();
      deleteMut.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const handleCreate = async () => {
    const name = newName.trim();
    if (!name) return;
    try {
      await createMut.mutateAsync({ name });
      setNewName("");
      pushToast({ msg: `已新建数据组「${name}」`, kind: "success" });
    } catch (err) {
      pushToast({
        msg: "创建失败",
        sub: err instanceof Error ? err.message : String(err),
        kind: "error",
      });
    }
  };

  const handleRename = async (g: GroupResponse) => {
    const name = editingName.trim();
    if (!name || name === g.name) {
      setEditingId(null);
      return;
    }
    try {
      await updateMut.mutateAsync({ id: g.id, payload: { name } });
      setEditingId(null);
      pushToast({ msg: "已重命名", kind: "success" });
    } catch (err) {
      pushToast({
        msg: "重命名失败",
        sub: err instanceof Error ? err.message : String(err),
        kind: "error",
      });
    }
  };

  const handleDelete = async (g: GroupResponse) => {
    try {
      await deleteMut.mutateAsync(g.id);
      setPendingDelete(null);
      pushToast({ msg: `已删除「${g.name}」`, kind: "success" });
    } catch (err) {
      pushToast({
        msg: "删除失败",
        sub: err instanceof Error ? err.message : String(err),
        kind: "error",
      });
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="管理数据组" width={560}>
      <div className={styles.root}>
        <div className={styles.createRow}>
          <input
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="新建数据组（如：标注组D）"
            maxLength={100}
            className={styles.input}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                handleCreate();
              }
            }}
          />
          <Button
            variant="primary"
            onClick={handleCreate}
            disabled={createMut.isPending || !newName.trim()}
          >
            <Icon name="plus" size={12} /> 新建
          </Button>
        </div>

        {isLoading && <div className={styles.loading}>加载中…</div>}

        {!isLoading && groups.length === 0 && (
          <div className={styles.empty}>暂无数据组，输入名称后点击「新建」</div>
        )}

        {!isLoading && groups.length > 0 && (
          <div className={styles.list}>
            {pagination.items.map((g) => {
              const isEditing = editingId === g.id;
              const isPending = pendingDelete === g.id;
              return (
                <div key={g.id} className={`${styles.row} ${listStyles.row}`}>
                  <Icon name="folder" size={14} className={styles.mutedIcon} />
                  {isEditing ? (
                    <input
                      autoFocus
                      value={editingName}
                      onChange={(e) => setEditingName(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") handleRename(g);
                        if (e.key === "Escape") setEditingId(null);
                      }}
                      className={`${styles.input} ${styles.grow}`}
                    />
                  ) : (
                    <span className={styles.grow}>{g.name}</span>
                  )}
                  <span className={styles.count}>{g.member_count} 人</span>
                  {isEditing ? (
                    <>
                      <Button
                        size="sm"
                        variant="primary"
                        onClick={() => handleRename(g)}
                        disabled={updateMut.isPending}
                      >
                        保存
                      </Button>
                      <Button size="sm" onClick={() => setEditingId(null)}>
                        取消
                      </Button>
                    </>
                  ) : isPending ? (
                    <>
                      <span className={styles.deleteConfirm}>确认删除？</span>
                      <Button
                        size="sm"
                        variant="danger"
                        onClick={() => handleDelete(g)}
                        disabled={deleteMut.isPending}
                      >
                        删除
                      </Button>
                      <Button size="sm" onClick={() => setPendingDelete(null)}>
                        取消
                      </Button>
                    </>
                  ) : (
                    <>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => {
                          setEditingId(g.id);
                          setEditingName(g.name);
                        }}
                      >
                        <Icon name="edit" size={11} />
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => setPendingDelete(g.id)}>
                        <Icon name="trash" size={11} />
                      </Button>
                    </>
                  )}
                </div>
              );
            })}
          </div>
        )}

        <ListPagination
          label="数据组"
          page={pagination.page}
          pageSize={pagination.pageSize}
          total={pagination.total}
          onPageChange={pagination.setPage}
          onPageSizeChange={pagination.setPageSize}
        />

        <div className={styles.actions}>
          <Button variant="primary" onClick={onClose}>
            完成
          </Button>
        </div>
      </div>
    </Modal>
  );
}
