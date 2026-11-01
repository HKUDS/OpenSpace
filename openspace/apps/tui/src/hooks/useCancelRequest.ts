import { useKeybinding } from "../keybindings/useKeybinding.js";

type UseCancelRequestProps = {
  isActive: boolean;
  onCancel: () => void;
};

export function useCancelRequest({
  isActive,
  onCancel,
}: UseCancelRequestProps): void {
  useKeybinding("chat:cancel", onCancel, {
    context: "Chat",
    isActive,
  });
}
