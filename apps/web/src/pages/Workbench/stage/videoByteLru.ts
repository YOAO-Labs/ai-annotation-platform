export interface ByteLruEntry<V> {
  value: V;
  bytes: number;
  dispose?: (value: V) => void;
}

interface ByteLruNode<V> {
  value: V;
  bytes: number;
  dispose?: (value: V) => void;
}

function assertBudgetBytes(budgetBytes: number): void {
  if (!Number.isFinite(budgetBytes) || budgetBytes < 0) {
    throw new Error("ByteLru budgetBytes must be a non-negative finite number");
  }
}

function assertEntryBytes(bytes: number): void {
  if (!Number.isFinite(bytes) || bytes < 0) {
    throw new Error("ByteLru entry bytes must be a non-negative finite number");
  }
}

export class ByteLru<K, V> {
  private readonly map = new Map<K, ByteLruNode<V>>();
  private _bytes = 0;
  private _budgetBytes: number;
  private _evictions = 0;

  constructor(budgetBytes: number) {
    assertBudgetBytes(budgetBytes);
    this._budgetBytes = budgetBytes;
  }

  get budgetBytes(): number {
    return this._budgetBytes;
  }

  get bytes(): number {
    return this._bytes;
  }

  get size(): number {
    return this.map.size;
  }

  get evictions(): number {
    return this._evictions;
  }

  has(key: K): boolean {
    return this.map.has(key);
  }

  get(key: K): V | undefined {
    const node = this.map.get(key);
    if (!node) return undefined;
    this.map.delete(key);
    this.map.set(key, node);
    return node.value;
  }

  peek(key: K): V | undefined {
    return this.map.get(key)?.value;
  }

  take(key: K): ByteLruEntry<V> | undefined {
    const node = this.map.get(key);
    if (!node) return undefined;
    this.map.delete(key);
    this._bytes -= node.bytes;
    return {
      value: node.value,
      bytes: node.bytes,
      dispose: node.dispose,
    };
  }

  set(key: K, entry: ByteLruEntry<V>): boolean {
    assertEntryBytes(entry.bytes);
    const existing = this.map.get(key);
    if (existing) {
      this._bytes -= existing.bytes;
      this.map.delete(key);
      existing.dispose?.(existing.value);
    }
    if (entry.bytes > this._budgetBytes) {
      return false;
    }
    const node: ByteLruNode<V> = {
      value: entry.value,
      bytes: entry.bytes,
      dispose: entry.dispose,
    };
    this.map.set(key, node);
    this._bytes += node.bytes;
    this.evict();
    return true;
  }

  delete(key: K): boolean {
    const node = this.map.get(key);
    if (!node) return false;
    this._bytes -= node.bytes;
    this.map.delete(key);
    node.dispose?.(node.value);
    return true;
  }

  clear(): void {
    for (const node of this.map.values()) {
      node.dispose?.(node.value);
    }
    this.map.clear();
    this._bytes = 0;
  }

  setBudget(budgetBytes: number): number {
    assertBudgetBytes(budgetBytes);
    this._budgetBytes = budgetBytes;
    return this.evict();
  }

  private evict(): number {
    let evicted = 0;
    while (this._bytes > this._budgetBytes && this.map.size > 0) {
      const oldest = this.map.keys().next();
      if (oldest.done) break;
      const key = oldest.value as K;
      const node = this.map.get(key);
      if (!node) break;
      this._bytes -= node.bytes;
      this.map.delete(key);
      node.dispose?.(node.value);
      evicted += 1;
    }
    this._evictions += evicted;
    return evicted;
  }

  entries(): IterableIterator<[K, V]> {
    return (function* (src: Map<K, ByteLruNode<V>>): IterableIterator<[K, V]> {
      for (const [k, n] of src) yield [k, n.value] as [K, V];
    })(this.map);
  }

  keys(): IterableIterator<K> {
    return this.map.keys();
  }

  values(): IterableIterator<V> {
    return (function* (src: Map<K, ByteLruNode<V>>): IterableIterator<V> {
      for (const n of src.values()) yield n.value;
    })(this.map);
  }
}
