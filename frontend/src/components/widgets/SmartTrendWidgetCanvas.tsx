import { useState, useMemo } from 'react';
import { useMultiOuraQuery } from '@/hooks/useMultiOuraQuery';
import { TrendChartCanvas } from './TrendChartCanvas';
import { BarChartCanvas } from './BarChartCanvas';
import { TableWidget } from './TableWidget';
import type { WidgetInstance } from '@/types';
import { subDays, subYears, format } from 'date-fns';

import { isIntradayKey } from "@/lib/utils";
import { aggregateDailySeries, normalizeTimeSeriesData, pickAutoAggregationInterval } from '@/lib/data-processing';

interface SmartTrendWidgetCanvasProps {
    widget: WidgetInstance;
    date: string; // End date

    chartType?: 'area' | 'bar' | 'table';
    onUpdate?: (updates: Partial<WidgetInstance>) => void;
}

export function SmartTrendWidgetCanvas({ widget, date, chartType = 'area' }: SmartTrendWidgetCanvasProps) {
    // Calculate date range based on config
    const { startDate, endDate } = useMemo(() => {
        const primaryKey = widget.config.dataKey || '';
        if (primaryKey && isIntradayKey(primaryKey)) {
            // Force date-only format to avoid start_datetime/end_datetime queries which cause freezes
            const dateOnly = date.split('T')[0];
            return { startDate: dateOnly, endDate: dateOnly };
        }

        // For Table view, strictly fetch only the selected date to match the UI
        if (chartType === 'table') {
            const dateOnly = date.split('T')[0];
            return { startDate: dateOnly, endDate: dateOnly };
        }

        const rangeType = widget.config.dateRange?.type || 'default';
        const today = new Date().toISOString().split('T')[0];

        switch (rangeType) {
            case 'custom':
                return {
                    startDate: widget.config.dateRange?.startDate || format(subDays(new Date(date), 30), 'yyyy-MM-dd'),
                    endDate: widget.config.dateRange?.endDate || date
                };
            case 'relative': {
                const { value, unit, anchor } = widget.config.dateRange || {};

                let end;
                if (anchor === 'selected_date') {
                    end = new Date(date);
                } else {
                    // Always use today as anchor. NOTE: must be `new Date()` —
                    // `new Date('yyyy-MM-dd')` parses as UTC midnight, which
                    // format() shifts to YESTERDAY in negative-UTC timezones.
                    end = new Date();
                }

                if (value && unit) {
                    if (unit === 'hours' || unit === 'minutes') {
                        // Sub-day windows: the API filters by whole days, so
                        // fetch yesterday..today and fine-filter client-side
                        // (see timeFilteredData below).
                        return {
                            startDate: format(subDays(end, 1), 'yyyy-MM-dd'),
                            endDate: format(end, 'yyyy-MM-dd')
                        };
                    }
                    let start = end;
                    if (unit === 'days') start = subDays(end, value);
                    else if (unit === 'weeks') start = subDays(end, value * 7);
                    else if (unit === 'months') start = subDays(end, value * 30);
                    else if (unit === 'years') start = subYears(end, value);

                    return {
                        startDate: format(start, 'yyyy-MM-dd'),
                        endDate: format(end, 'yyyy-MM-dd')
                    };
                }

                return {
                    startDate: format(subDays(end, 7), 'yyyy-MM-dd'),
                    endDate: format(end, 'yyyy-MM-dd')
                };
            }
            case 'selected_day': {
                // Exactly the selected day (defaults to today) — nothing else.
                const dayOnly = (date || new Date().toISOString()).split('T')[0];
                return { startDate: dayOnly, endDate: dayOnly };
            }
            case 'to_today':
                return {
                    startDate: widget.config.dateRange?.startDate || format(subDays(new Date(), 30), 'yyyy-MM-dd'),
                    endDate: today
                };
            case 'all':
                return { startDate: undefined, endDate: undefined };
            case 'last_90':
                return { startDate: format(subDays(new Date(), 90), 'yyyy-MM-dd'), endDate: today };
            case 'last_30':
                return { startDate: format(subDays(new Date(), 30), 'yyyy-MM-dd'), endDate: today };
            default:
                // Default to last 7 days relative to today for new widgets
                return { startDate: format(subDays(new Date(), 7), 'yyyy-MM-dd'), endDate: today };
        }
    }, [widget.config.dateRange, date, widget.config.dataKey]);

    // Determine keys to fetch
    const keysToFetch = useMemo(() => {
        if (widget.config.dataKeys && widget.config.dataKeys.length > 0) {
            return widget.config.dataKeys;
        }
        return widget.config.dataKey ? [widget.config.dataKey] : [];
    }, [widget.config.dataKeys, widget.config.dataKey]);

    const { data, loading, error } = useMultiOuraQuery(keysToFetch, startDate, endDate);

    // Intraday Logic (Simplified for Canvas Test)
    const [selectedDayIndex] = useState<number | null>(null);

    const processedData = useMemo(() => {
        if (keysToFetch.length === 0) return { data: [], isIntraday: false };

        const primaryKey = keysToFetch[0];
        // Heuristic: If we are plotting sleep (hypnogram or detailed sleep), we want the chart
        // to only show the "in-bed" period, not the full 24h day with empty space.
        // Passing undefined for start/end lets normalizeTimeSeriesData use the data's own timestamps.
        const isSleepDetailed = (primaryKey.includes('sleep') || primaryKey.includes('hypnogram')) && isIntradayKey(primaryKey);

        return normalizeTimeSeriesData(
            data,
            primaryKey,
            selectedDayIndex,
            isSleepDetailed ? undefined : startDate,
            isSleepDetailed ? undefined : endDate
        );
    }, [data, selectedDayIndex, keysToFetch, startDate, endDate]);

    // Sub-day (hours/minutes) relative windows: fine-filter fetched points
    // client-side, since the API only supports whole-day bounds.
    const timeFilteredData = useMemo(() => {
        const dr = widget.config.dateRange;
        if (dr?.type !== 'relative' || !dr.value ||
            (dr.unit !== 'hours' && dr.unit !== 'minutes')) {
            return processedData;
        }
        const unitMs = dr.unit === 'hours' ? 3_600_000 : 60_000;
        const cutoff = Date.now() - dr.value * unitMs;
        return {
            ...processedData,
            data: processedData.data.filter((p: any) => {
                const t = new Date(p.date).getTime();
                return !isNaN(t) && t >= cutoff;
            })
        };
    }, [processedData, widget.config.dateRange]);

    const aggregatedData = useMemo(() => {
        if (!timeFilteredData.data.length) return timeFilteredData;
        if (timeFilteredData.isIntraday) return timeFilteredData;

        const rangeType = widget.config.dateRange?.type;
        if (rangeType !== 'all') return timeFilteredData;

        const interval = pickAutoAggregationInterval(timeFilteredData.data);
        if (!interval) return timeFilteredData;

        return {
            ...timeFilteredData,
            data: aggregateDailySeries(timeFilteredData.data, keysToFetch, interval, 'avg')
        };
    }, [timeFilteredData, keysToFetch, widget.config.dateRange?.type]);

    if (keysToFetch.length === 0) {
        return (
            <div className="flex flex-col items-center justify-center h-full text-muted-foreground p-4 text-center">
                <span className="text-sm font-medium">No data selected</span>
                <span className="text-xs opacity-70 mt-1">Edit widget to select data</span>
            </div>
        );
    }

    if (loading) return <div className="flex items-center justify-center h-full text-xs text-muted-foreground">Loading...</div>;
    if (error) return <div className="flex items-center justify-center h-full text-xs text-destructive">Error: {error}</div>;

    if (aggregatedData.data.length === 0) {
        return (
            <div className="flex flex-col items-center justify-center h-full text-muted-foreground p-4 text-center">
                <span className="text-sm font-medium">No data for this period</span>
                <span className="text-xs opacity-70 mt-1">Try selecting a different date range</span>
            </div>
        );
    }



    return (
        <div className="h-full flex flex-col relative group">
            {chartType === 'bar' ? (
                <BarChartCanvas
                    data={aggregatedData.data}
                    dataKey={keysToFetch[0]}
                    categoryKey="date"
                    color={widget.config.color || '#8AB4F8'}
                />
            ) : chartType === 'table' ? (
                <TableWidget
                    data={aggregatedData.data}
                    dataKeys={keysToFetch}
                    selectedDate={date}
                />
            ) : (
                <TrendChartCanvas
                    data={aggregatedData.data}
                    dataKey={keysToFetch[0]}
                    dataKeys={keysToFetch}
                    title={widget.title}
                    color={widget.config.color || '#8AB4F8'}
                    showPoints={widget.config.showPoints}
                />
            )}

        </div>
    );
}
