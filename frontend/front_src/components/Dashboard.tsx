import { useState, useEffect } from 'react';
import { BarChart3, LineChart as LineChartIcon, Info } from 'lucide-react';
import YieldCurveChart from './YieldCurveChart';
import ScenarioSelector from './ScenarioSelector';
import DateSelector from './DateSelector';
import {
    fetchAvailableDates,
    fetchProcessedCurveByDate,
    type BucketInfo,
} from './yieldCurveApi';
import { MOCK_DATA_ON } from '../mock_config';
import {
    AVAILABLE_DATES,
    BASE_CURVE,
    BUCKETS,
    STRESS_SCENARIOS,
    type StressScenario,
} from '../data/mockData';
import '../styles/Dashboard.css';

export default function Dashboard() {
    const [selectedDate, setSelectedDate] = useState('2026-01-15');
    const [dates, setDates] = useState<string[]>(AVAILABLE_DATES);
    const [buckets, setBuckets] = useState<BucketInfo[]>(BUCKETS);
    const [scenarios, setScenarios] = useState<StressScenario[]>(STRESS_SCENARIOS);
    const [currentCurve, setCurrentCurve] = useState<number[]>(BASE_CURVE);
    const [scenarioCurves, setScenarioCurves] = useState<Record<string, number[]>>({});
    const [activeScenarios, setActiveScenarios] = useState<string[]>([]);
    const [showBase, setShowBase] = useState(true);
    const [chartType, setChartType] = useState<'step' | 'scatter'>('step');
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        if (MOCK_DATA_ON) {
            setDates(AVAILABLE_DATES);
            return;
        }

        const controller = new AbortController();
        fetchAvailableDates({}, controller.signal)
            .then((loadedDates) => {
                if (loadedDates.length > 0) {
                    setDates(loadedDates);
                    if (!loadedDates.includes(selectedDate)) {
                        setSelectedDate(loadedDates[0]);
                    }
                }
            })
            .catch((err: unknown) => {
                if (err instanceof Error && err.name !== 'AbortError') {
                    setError(err.message);
                }
            });

        return () => controller.abort();
    }, []);

    useEffect(() => {
        const controller = new AbortController();
        setLoading(true);
        setError(null);

        fetchProcessedCurveByDate(selectedDate, undefined, controller.signal).then(
            (payload) => {
                if (controller.signal.aborted) return;
                setCurrentCurve(payload.baseCurve);
                setScenarioCurves(payload.scenarioCurves);
                if (payload.buckets.length) setBuckets(payload.buckets);
                if (payload.scenarios.length) setScenarios(payload.scenarios);
                if (payload.availableDates.length) setDates(payload.availableDates);
                setLoading(false);
            },
            (err: unknown) => {
                if (controller.signal.aborted) return;
                if (err instanceof Error && err.name !== 'AbortError') {
                    setError(err.message);
                }
                setLoading(false);
            }
        );

        return () => controller.abort();
    }, [selectedDate]);

    const handleToggleScenario = (id: string) => {
        setActiveScenarios((prev) =>
            prev.includes(id) ? prev.filter((s) => s !== id) : [...prev, id]
        );
    };

    const handleClearAll = () => setActiveScenarios([]);
    const handleSelectAll = () =>
        setActiveScenarios(scenarios.map((scenario) => scenario.id));

    return (
        <div className="dashboard">
            {/* Header */}
            <div className="dashboard-header">
                <div>
                    <h2 className="dashboard-title">
                        <BarChart3 className="dashboard-title-icon" />
                        Дашборд IRRBB
                    </h2>
                    <p className="dashboard-subtitle">
                        Процентный риск в банковской книге • {buckets.length} бакетов •{' '}
                        {scenarios.length} стресс-сценариев •{' '}
                        {MOCK_DATA_ON ? 'Мок-данные (MOCK_DATA_ON)' : 'Бэкенд /curves/processed'}
                    </p>
                </div>
                <div className="dashboard-controls">
                    <button
                        onClick={() => setChartType('step')}
                        className={`chart-type-btn ${chartType === 'step' ? 'active' : ''}`}
                        title="Ступенчатый график"
                    >
                        <BarChart3 className="chart-type-btn-icon" />
                    </button>
                    <button
                        onClick={() => setChartType('scatter')}
                        className={`chart-type-btn ${chartType === 'scatter' ? 'active' : ''}`}
                        title="Точечный график"
                    >
                        <LineChartIcon className="chart-type-btn-icon" />
                    </button>
                </div>
            </div>

            {error && (
                <div className="chart-info" role="alert">
                    <Info className="chart-info-icon" />
                    <p className="chart-info-text">{error}</p>
                </div>
            )}

            <div className="chart-card">
                <div className="chart-card-header">
                    <h3 className="chart-card-title">
                        Кривая доходности ({chartType === 'step' ? 'Step Chart' : 'Scatter Plot'})
                        {loading ? ' • Загрузка...' : ''}
                    </h3>
                    <label className="chart-checkbox">
                        <input
                            type="checkbox"
                            checked={showBase}
                            onChange={(e) => setShowBase(e.target.checked)}
                            className="chart-checkbox-input"
                        />
                        <span className="chart-checkbox-label">Базовая кривая</span>
                    </label>
                </div>
                <div className="chart-container">
                    <YieldCurveChart
                        baseCurve={currentCurve}
                        scenarioCurves={scenarioCurves}
                        scenarios={scenarios}
                        buckets={buckets}
                        activeScenarios={activeScenarios}
                        showBase={showBase}
                        chartType={chartType}
                    />
                </div>
                <div className="chart-info">
                    <Info className="chart-info-icon" />
                    <p className="chart-info-text">
                        Ось X пропорциональна времени: расстояние между 10Y и 20Y больше, чем между 1Y и 2Y.
                        Это обеспечивает корректную визуализацию временных бакетов без искажений.
                    </p>
                </div>
            </div>

            <div className="controls-grid">
                <DateSelector
                    selectedDate={selectedDate}
                    onDateChange={setSelectedDate}
                    availableDates={dates}
                />
                <ScenarioSelector
                    activeScenarios={activeScenarios}
                    scenarios={scenarios}
                    onToggle={handleToggleScenario}
                    onClearAll={handleClearAll}
                    onSelectAll={handleSelectAll}
                />
            </div>

            <div className="bucket-table-card">
                <h3 className="bucket-table-title">Таблица бакетов и значений</h3>
                <table className="bucket-table">
                    <thead>
                    <tr>
                        <th>Бакет</th>
                        <th>Диапазон</th>
                        <th className="right">Середина (лет)</th>
                        <th className="right">Ставка, %</th>
                        {activeScenarios.map((id) => {
                            const scenario = scenarios.find((s) => s.id === id);
                            return (
                                <th
                                    key={id}
                                    className="right"
                                    style={{ color: scenario?.color }}
                                >
                                    {scenario?.name}
                                </th>
                            );
                        })}
                    </tr>
                    </thead>
                    <tbody>
                    {buckets.map((bucket, i) => (
                        <tr key={bucket.id}>
                            <td className="mono white">{bucket.label}</td>
                            <td className="gray">
                                {bucket.startMonths}-{bucket.endMonths} мес.
                            </td>
                            <td className="right gray">{bucket.midpointYears}</td>
                            <td className="right mono white">
                                {(currentCurve[i] ?? 0).toFixed(2)}
                            </td>
                            {activeScenarios.map((scenarioId) => {
                                const scenario = scenarios.find((s) => s.id === scenarioId);
                                const baseVal = currentCurve[i] ?? 0;
                                const directVal = scenarioCurves[scenarioId]?.[i];
                                const value =
                                    directVal !== undefined
                                        ? directVal
                                        : baseVal + (scenario?.shifts[i] ?? 0) / 100;
                                return (
                                    <td
                                        key={scenarioId}
                                        className="right mono"
                                        style={{ color: scenario?.color }}
                                    >
                                        {Number(value).toFixed(2)}
                                    </td>
                                );
                            })}
                        </tr>
                    ))}
                    </tbody>
                </table>
            </div>
        </div>
    );
}