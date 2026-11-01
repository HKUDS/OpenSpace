import { createSignal } from "./signal.js";

export type MailboxMessageSource =
  | "user"
  | "teammate"
  | "system"
  | "tick"
  | "task";

export type MailboxMessage = {
  id: string;
  source: MailboxMessageSource;
  content: string;
  from?: string;
  color?: string;
  timestamp: string;
};

type Waiter = {
  fn: (message: MailboxMessage) => boolean;
  resolve: (message: MailboxMessage) => void;
};

export class Mailbox {
  private readonly queue: MailboxMessage[] = [];
  private readonly waiters: Waiter[] = [];
  private readonly changed = createSignal();
  private revisionValue = 0;

  get length(): number {
    return this.queue.length;
  }

  get revision(): number {
    return this.revisionValue;
  }

  send(message: MailboxMessage): void {
    this.revisionValue += 1;

    const waiterIndex = this.waiters.findIndex(waiter => waiter.fn(message));
    if (waiterIndex !== -1) {
      const waiter = this.waiters.splice(waiterIndex, 1)[0];
      waiter?.resolve(message);
      this.notify();
      return;
    }

    this.queue.push(message);
    this.notify();
  }

  poll(
    fn: (message: MailboxMessage) => boolean = () => true,
  ): MailboxMessage | undefined {
    const index = this.queue.findIndex(fn);
    if (index === -1) {
      return undefined;
    }

    return this.queue.splice(index, 1)[0];
  }

  receive(
    fn: (message: MailboxMessage) => boolean = () => true,
  ): Promise<MailboxMessage> {
    const index = this.queue.findIndex(fn);
    if (index !== -1) {
      const message = this.queue.splice(index, 1)[0];
      if (message) {
        this.notify();
        return Promise.resolve(message);
      }
    }

    return new Promise<MailboxMessage>(resolve => {
      this.waiters.push({ fn, resolve });
    });
  }

  subscribe = this.changed.subscribe;

  private notify(): void {
    this.changed.emit();
  }
}
