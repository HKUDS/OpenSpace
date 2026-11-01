export type LocalCommandResult =
  | {
      type: "text";
      value: string;
      display?: "system" | "user";
    }
  | {
      type: "clear";
      value?: string;
    }
  | {
      type: "input";
      value: string;
      submit?: boolean;
    }
  | {
      type: "skip";
    };

export type CommandAvailability = "local" | "core";

export type CommandBase = {
  availability?: CommandAvailability[];
  description: string;
  name: string;
  aliases?: string[];
  argumentHint?: string;
  isEnabled?: () => boolean;
  isHidden?: boolean;
  userFacingName?: () => string;
  category?: string;
};

export type Command = CommandBase & {
  handler: "local" | "core";
};

export function getCommandName(cmd: CommandBase): string {
  return cmd.userFacingName?.() ?? cmd.name;
}

export function isCommandEnabled(cmd: CommandBase): boolean {
  return cmd.isEnabled?.() ?? true;
}
