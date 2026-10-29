export interface UploadLimits {
  active_files: number;
  hash_workers: number;
  global_parts: number;
  parts_per_file: number;
}

const abortError = () => new DOMException("Paused", "AbortError");
type Release = () => void;
interface Waiter {
  signal: AbortSignal;
  cancel: () => void;
  resolve: (release: Release) => void;
  reject: (error: Error) => void;
}

function bounded(value: unknown, maximum: number) {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0
    ? Math.min(value, maximum)
    : maximum;
}

export function uploadLimits(value: Partial<UploadLimits> = {}): UploadLimits {
  const global_parts = bounded(value.global_parts, 10);
  const active_files = Math.min(bounded(value.active_files, 10), global_parts);
  return {
    active_files,
    hash_workers: Math.min(bounded(value.hash_workers, 2), active_files),
    global_parts,
    parts_per_file: Math.min(bounded(value.parts_per_file, 3), global_parts),
  };
}

/** FIFO acquisition, with idempotent release and removable cancelled waiters. */
class Semaphore {
  active = 0;
  readonly waiting: Waiter[] = [];
  constructor(readonly capacity: number) {}

  acquire(signal: AbortSignal): Promise<Release> {
    if (signal.aborted) return Promise.reject(abortError());
    return new Promise((resolve, reject) => {
      const waiter: Waiter = {
        signal,
        resolve,
        reject,
        cancel: () => {
          const index = this.waiting.indexOf(waiter);
          if (index >= 0) this.waiting.splice(index, 1);
          signal.removeEventListener("abort", waiter.cancel);
          reject(abortError());
          this.drain();
        },
      };
      signal.addEventListener("abort", waiter.cancel, { once: true });
      this.waiting.push(waiter);
      this.drain();
    });
  }

  private drain() {
    while (this.active < this.capacity && this.waiting.length) {
      const waiter = this.waiting.shift()!;
      waiter.signal.removeEventListener("abort", waiter.cancel);
      if (waiter.signal.aborted) {
        waiter.reject(abortError());
        continue;
      }
      this.active++;
      let released = false;
      waiter.resolve(() => {
        if (released) return;
        released = true;
        this.active--;
        this.drain();
      });
    }
  }
}

interface Owner {
  held: number;
  closed: boolean;
  waiting: Waiter[];
}

/** Round-robin part ownership, reserving a first slot for each admitted file.
 * Hashing can finish at different times: an early file must not monopolize the
 * entire pool while the other admitted files are still checking their bytes.
 */
class PartPool {
  active = 0;
  readonly owners = new Map<string, Owner>();
  private next = 0;
  constructor(
    readonly capacity: number,
    readonly perFile: number,
  ) {}

  register(key: string): Release {
    if (this.owners.has(key)) throw new Error("Upload already registered");
    const owner: Owner = { held: 0, closed: false, waiting: [] };
    this.owners.set(key, owner);
    this.drain();
    return () => {
      if (owner.closed) return;
      owner.closed = true;
      for (const waiter of owner.waiting.splice(0)) {
        waiter.signal.removeEventListener("abort", waiter.cancel);
        waiter.reject(abortError());
      }
      if (!owner.held) this.owners.delete(key);
      this.drain();
    };
  }

  acquire(key: string, signal: AbortSignal): Promise<Release> {
    const owner = this.owners.get(key);
    if (signal.aborted || !owner || owner.closed)
      return Promise.reject(abortError());
    return new Promise((resolve, reject) => {
      const waiter: Waiter = {
        signal,
        resolve,
        reject,
        cancel: () => {
          const index = owner.waiting.indexOf(waiter);
          if (index >= 0) owner.waiting.splice(index, 1);
          signal.removeEventListener("abort", waiter.cancel);
          reject(abortError());
          this.drain();
        },
      };
      signal.addEventListener("abort", waiter.cancel, { once: true });
      owner.waiting.push(waiter);
      this.drain();
    });
  }

  private drain() {
    while (this.active < this.capacity) {
      const keys = [...this.owners.keys()];
      let selected = -1;
      for (let step = 0; step < keys.length; step++) {
        const index = (this.next + step) % keys.length;
        const owner = this.owners.get(keys[index])!;
        if (owner.closed || !owner.waiting.length || owner.held >= this.perFile)
          continue;
        const reserved = [...this.owners.values()].filter(
          (other) => other !== owner && !other.closed && other.held === 0,
        ).length;
        if (this.capacity - this.active <= reserved) continue;
        selected = index;
        break;
      }
      if (selected < 0) return;
      const key = keys[selected];
      const owner = this.owners.get(key)!;
      const waiter = owner.waiting.shift()!;
      waiter.signal.removeEventListener("abort", waiter.cancel);
      this.next = (selected + 1) % keys.length;
      if (waiter.signal.aborted) {
        waiter.reject(abortError());
        continue;
      }
      owner.held++;
      this.active++;
      let released = false;
      waiter.resolve(() => {
        if (released) return;
        released = true;
        owner.held--;
        this.active--;
        if (owner.closed && !owner.held) this.owners.delete(key);
        this.drain();
      });
    }
  }
}

export class UploadScheduler {
  readonly limits: UploadLimits;
  private readonly files: Semaphore;
  private readonly hashes: Semaphore;
  private readonly parts: PartPool;

  constructor(limits: Partial<UploadLimits> = {}) {
    this.limits = uploadLimits(limits);
    this.files = new Semaphore(this.limits.active_files);
    this.hashes = new Semaphore(this.limits.hash_workers);
    this.parts = new PartPool(
      this.limits.global_parts,
      this.limits.parts_per_file,
    );
  }

  async acquireFile(key: string, signal: AbortSignal): Promise<Release> {
    const release = await this.files.acquire(signal);
    if (signal.aborted) {
      release();
      throw abortError();
    }
    const unregister = this.parts.register(key);
    let released = false;
    return () => {
      if (released) return;
      released = true;
      unregister();
      release();
    };
  }

  acquireHash(signal: AbortSignal) {
    return this.hashes.acquire(signal);
  }

  acquirePart(key: string, signal: AbortSignal) {
    return this.parts.acquire(key, signal);
  }

  snapshot() {
    return {
      active_files: this.files.active,
      queued_files: this.files.waiting.length,
      hash_workers: this.hashes.active,
      queued_hashes: this.hashes.waiting.length,
      global_parts: this.parts.active,
      queued_parts: [...this.parts.owners.values()].reduce(
        (total, owner) => total + owner.waiting.length,
        0,
      ),
    };
  }
}
