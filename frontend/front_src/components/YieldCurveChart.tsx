import { useMemo } from 'react';
import {
    LineChart,
    Line,
    XAxis,
    YAxis,
    CartesianGrid,
    Tooltip,
    Legend,
    ResponsiveContainer,
} from 'recharts';
import {
    BUCKETS,
    STRESS_SCENARIOS,
    type StressScenario,
} from '../data/mockData';
import type { BucketInfo } from './yieldCurveApi';
import '../styles/YieldCurveChart.css';

interface YieldCurveChartProps {
    baseCurve: number[];
    scenarioCurves?: Record<string, number[]>;
    scenarios?: StressScenario[];
    buckets?: BucketInfo[];
    activeScenarios: string[];
    showBase: boolean;
    chartType: 'step' | 'scatter';
}

interface DataPoint {
    x: number;
    label: string;
    base: number;
    [key: string]: number | string;
}

export default function YieldCurveChart({
                                            baseCurve,
                                            scenarioCurves,
                                            scenarios = STRESS_SCENARIOS,
                                            buckets = BUCKETS,
                                            activeScenarios,
                                            showBase,
                                            chartType,
                                        }: YieldCurveChartProps) {
    const chartData = useMemo(() => {
        return buckets.map((bucket, i) => {
            const baseVal = baseCurve[i] ?? 0;
            const point: DataPoint = {
                x: bucket.midpointYears,
                label: bucket.label,
                base: baseVal,
            };

            activeScenarios.forEach((scenarioId) => {
                const scenario = scenarios.find((s) => s.id === scenarioId);
                if (!scenario) return;

                const directCurve = scenarioCurves?.[scenarioId];
                const value =
                    directCurve && directCurve[i] !== undefined
                        ? directCurve[i]
                        : baseVal + (scenario.shifts[i] ?? 0) / 100;

                point[scenarioId] = +Number(value).toFixed(2);
            });

            return point;
        });
    }, [buckets, baseCurve, scenarioCurves, scenarios, activeScenarios]);

    const activeScenarioObjects = useMemo(() => {
        return scenarios.filter((s) => activeScenarios.includes(s.id));
    }, [scenarios, activeScenarios]);

    const xTicks = useMemo(() => buckets.map((b) => b.midpointYears), [buckets]);
    const maxDomainX = useMemo(() => {
        const maxVal = Math.max(...xTicks, 42);
        return Math.ceil(maxVal);
    }, [xTicks]);

    const CustomTooltip = ({ active, payload, label }: any) => {
        if (active && payload && payload.length) {
            const bucket = buckets.find((b) => b.midpointYears === label);
            return (
                <div className="chart-tooltip">
                    <p className="chart-tooltip-title">Бакет: {bucket?.label}</p>
                    <p className="chart-tooltip-subtitle">
                        Диапазон: {bucket?.startMonths}-{bucket?.endMonths} мес.
                    </p>
                    {payload.map((entry: any, index: number) => {
                        const labelName =
                            entry.name === 'base'
                                ? 'Базовая кривая'
                                : scenarios.find((s) => s.id === entry.name)?.nameRu ||
                                entry.name;
                        const numVal = Number(entry.value ?? 0);
                        return (
                            <p
                                key={index}
                                style={{ color: entry.color }}
                                className="chart-tooltip-item"
                            >
                                {labelName}: {numVal.toFixed(2)}%
                            </p>
                        );
                    })}
                </div>
            );
        }
        return null;
    };

    const renderXAxisTick = (value: number) => {
        const bucket = buckets.find((b) => b.midpointYears === value);
        return bucket ? bucket.label : '';
    };

    const lineType = chartType === 'step' ? 'stepAfter' : 'monotone';

    return (
        <div className="yield-chart">
            <ResponsiveContainer width="100%" height="100%">
                <LineChart
                    data={chartData}
                    margin={{ top: 20, right: 30, left: 20, bottom: 60 }}
                >
                    <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
                    <XAxis
                        dataKey="x"
                        type="number"
                        domain={[0, maxDomainX]}
                        ticks={xTicks}
                        tickFormatter={renderXAxisTick}
                        stroke="#9ca3af"
                        tick={{ fontSize: 10, fill: '#9ca3af' }}
                        angle={-45}
                        textAnchor="end"
                        height={80}
                        label={{
                            value: 'Срок (бакеты)',
                            position: 'insideBottom',
                            offset: -5,
                            style: { fill: '#9ca3af', fontSize: 12 },
                        }}
                    />
                    <YAxis
                        stroke="#9ca3af"
                        tick={{ fontSize: 11, fill: '#9ca3af' }}
                        label={{
                            value: 'Ставка, %',
                            angle: -90,
                            position: 'insideLeft',
                            style: { fill: '#9ca3af', fontSize: 12 },
                        }}
                        domain={['auto', 'auto']}
                    />
                    <Tooltip content={<CustomTooltip />} />
                    <Legend
                        wrapperStyle={{ paddingTop: '10px' }}
                        formatter={(value: string) => {
                            if (value === 'base') return 'Базовая кривая';
                            const scenario = scenarios.find((s) => s.id === value);
                            return scenario?.nameRu || value;
                        }}
                    />
                    {showBase && (
                        <Line
                            type={lineType}
                            dataKey="base"
                            stroke="#f8fafc"
                            strokeWidth={2.5}
                            dot={
                                chartType === 'step'
                                    ? { fill: '#f8fafc', r: 3 }
                                    : { fill: '#f8fafc', r: 5, strokeWidth: 2 }
                            }
                            name="base"
                            connectNulls
                        />
                    )}
                    {activeScenarioObjects.map((scenario: StressScenario) => (
                        <Line
                            key={scenario.id}
                            type={lineType}
                            dataKey={scenario.id}
                            stroke={scenario.color}
                            strokeWidth={2}
                            strokeDasharray={chartType === 'step' ? '5 5' : undefined}
                            dot={
                                chartType === 'step'
                                    ? { fill: scenario.color, r: 2.5 }
                                    : { fill: scenario.color, r: 4, strokeWidth: 1 }
                            }
                            name={scenario.id}
                            connectNulls
                        />
                    ))}
                </LineChart>
            </ResponsiveContainer>
        </div>
    );
}