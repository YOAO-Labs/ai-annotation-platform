import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";

export interface UrlStateCodec<T> {
  parse: (search: URLSearchParams) => { state: T; issues: UrlStateIssue[] };
  encode: (current: URLSearchParams, state: T) => URLSearchParams;
  clear?: (current: URLSearchParams, defaults: T) => URLSearchParams;
}

export interface UrlStateIssue {
  key: string;
  message: string;
}

export interface UseUrlFilterStateOptions<T> {
  codec: UrlStateCodec<T>;
  defaults: T;
  ownedKeys?: readonly string[];
}

export interface UseUrlFilterStateResult<T> {
  state: T;
  issues: UrlStateIssue[];
  patch: (update: Partial<T> | ((previous: T) => T), options?: { replace?: boolean }) => void;
  reset: (options?: { replace?: boolean }) => void;
}

export function useUrlFilterState<T>({
  codec,
  defaults,
}: UseUrlFilterStateOptions<T>): UseUrlFilterStateResult<T> {
  const [searchParams, setSearchParams] = useSearchParams();
  const search = searchParams.toString();
  const decoded = useMemo(() => codec.parse(new URLSearchParams(search)), [codec, search]);
  const patch = useCallback(
    (update: Partial<T> | ((previous: T) => T), options: { replace?: boolean } = {}) => {
      setSearchParams(
        (current) => {
          const previous = codec.parse(current).state;
          const next = typeof update === "function" ? update(previous) : { ...previous, ...update };
          return codec.encode(current, next);
        },
        { replace: options.replace ?? true },
      );
    },
    [codec, setSearchParams],
  );
  const reset = useCallback(
    (options: { replace?: boolean } = {}) => {
      setSearchParams(
        (current) => codec.clear?.(current, defaults) ?? codec.encode(current, defaults),
        { replace: options.replace ?? true },
      );
    },
    [codec, defaults, setSearchParams],
  );
  return { state: decoded.state, issues: decoded.issues, patch, reset };
}
