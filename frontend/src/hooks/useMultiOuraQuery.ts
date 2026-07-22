import { useState, useEffect, useRef } from 'react';

import { api } from "@/lib/api";
import { useRefreshTick } from './useRefreshTick';

interface QueryResult {
    date: string;
    value: unknown;
}

type MergedRow = { date: string; timestamp: string } & Record<string, unknown>;

export function useMultiOuraQuery(paths: string[], startDate?: string, endDate?: string, refreshKey: number = 0) {
    const [data, setData] = useState<MergedRow[]>([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const tick = useRefreshTick();
    const hasLoaded = useRef(false);

    // Stable key so the effect only re-runs when the actual path list changes,
    // not when the caller passes a new array identity each render.
    const pathsKey = JSON.stringify(paths ?? []);

    useEffect(() => {
        const effectPaths: string[] = JSON.parse(pathsKey);
        if (effectPaths.length === 0) {
            setData([]);
            return;
        }

        const fetchData = async () => {
            // Spinner only on first load; background refreshes are silent.
            if (!hasLoaded.current) setLoading(true);
            setError(null);
            try {

                // Fetch all paths in parallel
                const promises = effectPaths.map(async (path) => {
                    const data = await api.getQuery(path, startDate, endDate);
                    return { path, data: data as QueryResult[] };
                });

                const results = await Promise.all(promises);

                // Merge data by date
                const mergedMap = new Map<string, MergedRow>();

                results.forEach(({ path, data }) => {
                    data.forEach(item => {
                        const dateKey = item.date;
                        if (!mergedMap.has(dateKey)) {
                            mergedMap.set(dateKey, {
                                date: dateKey,
                                timestamp: dateKey // Ensure timestamp exists for charts
                            });
                        }
                        const entry = mergedMap.get(dateKey)!;

                        entry[path] = item.value;
                    });
                });

                // Convert map to array and sort by date
                const mergedArray = Array.from(mergedMap.values()).sort((a, b) =>
                    new Date(a.date).getTime() - new Date(b.date).getTime()
                );

                setData(mergedArray);
                hasLoaded.current = true;
            } catch (err) {
                setError(err instanceof Error ? err.message : 'Unknown error');
                console.error("Multi Query Error:", err);
            } finally {
                setLoading(false);
            }
        };

        fetchData();
    }, [pathsKey, startDate, endDate, refreshKey, tick]);

    return { data, loading, error };
}
