import type { StorageBindingOut } from "@/api/generated/types.gen";
import { StorageBindingChangeButton } from "./StorageBindingChangeButton";

export function AvatarBindingChangeButton(props: {
  binding: StorageBindingOut;
  onApplied: () => Promise<void>;
}) {
  return <StorageBindingChangeButton purpose="avatars" {...props} />;
}
