import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { searchApi, type SearchResponse } from "@/api/search";

const EMPTY: SearchResponse = { projects: [], tasks: [], datasets: [], members: [] };

export function useGlobalSearch(input: string, limit = 5) {
  const [debounced, setDebounced] = useState(input);

  useEffect(() => {
    const t = setTimeout(() => setDebounced(input.trim()), 200);
    return () => clearTimeout(t);
  }, [input]);

  const query = useQuery({
    queryKey: ["global-search", debounced, limit],
    queryFn: ({ signal }) => searchApi.query(debounced, limit, { signal }),
    enabled: debounced.length > 0,
    staleTime: 30 * 1000,
  });

  return {
    data: query.data ?? EMPTY,
    isLoading: query.isFetching && debounced.length > 0,
    debounced,
  };
}
