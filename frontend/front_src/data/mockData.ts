// IRRBB Buckets - 19 стандартных временных бакетов по Базелю
export interface Bucket {
    id: number;
    label: string;
    startMonths: number;
    endMonths: number;
    midpointYears: number;
}

export const BUCKETS: Bucket[] = [
    { id: 1, label: '0-1M', startMonths: 0, endMonths: 1, midpointYears: 0.04 },
    { id: 2, label: '1-3M', startMonths: 1, endMonths: 3, midpointYears: 0.17 },
    { id: 3, label: '3-6M', startMonths: 3, endMonths: 6, midpointYears: 0.375 },
    { id: 4, label: '6-9M', startMonths: 6, endMonths: 9, midpointYears: 0.625 },
    { id: 5, label: '9-12M', startMonths: 9, endMonths: 12, midpointYears: 0.875 },
    { id: 6, label: '1-2Y', startMonths: 12, endMonths: 24, midpointYears: 1.5 },
    { id: 7, label: '2-3Y', startMonths: 24, endMonths: 36, midpointYears: 2.5 },
    { id: 8, label: '3-4Y', startMonths: 36, endMonths: 48, midpointYears: 3.5 },
    { id: 9, label: '4-5Y', startMonths: 48, endMonths: 60, midpointYears: 4.5 },
    { id: 10, label: '5-6Y', startMonths: 60, endMonths: 72, midpointYears: 5.5 },
    { id: 11, label: '6-7Y', startMonths: 72, endMonths: 84, midpointYears: 6.5 },
    { id: 12, label: '7-8Y', startMonths: 84, endMonths: 96, midpointYears: 7.5 },
    { id: 13, label: '8-9Y', startMonths: 96, endMonths: 108, midpointYears: 8.5 },
    { id: 14, label: '9-10Y', startMonths: 108, endMonths: 120, midpointYears: 9.5 },
    { id: 15, label: '10-15Y', startMonths: 120, endMonths: 180, midpointYears: 12.5 },
    { id: 16, label: '15-20Y', startMonths: 180, endMonths: 240, midpointYears: 17.5 },
    { id: 17, label: '20-25Y', startMonths: 240, endMonths: 300, midpointYears: 22.5 },
    { id: 18, label: '25-30Y', startMonths: 300, endMonths: 360, midpointYears: 27.5 },
    { id: 19, label: '>30Y', startMonths: 360, endMonths: 600, midpointYears: 40 },
];

// 6 стресс-сценариев Базель III/IV
export interface StressScenario {
    id: string;
    name: string;
    nameRu: string;
    color: string;
    shifts: number[];
}

export const STRESS_SCENARIOS: StressScenario[] = [
    {
        id: 'parallel_up',
        name: 'Parallel Up',
        nameRu: 'Параллельный сдвиг вверх',
        color: '#ef4444',
        shifts: [150, 150, 150, 150, 150, 150, 150, 150, 150, 150, 150, 150, 150, 150, 150, 150, 150, 150, 150],
    },
    {
        id: 'parallel_down',
        name: 'Parallel Down',
        nameRu: 'Параллельный сдвиг вниз',
        color: '#3b82f6',
        shifts: [-150, -150, -150, -150, -150, -150, -150, -150, -150, -150, -150, -150, -150, -150, -150, -150, -150, -150, -150],
    },
    {
        id: 'steepener',
        name: 'Steepener',
        nameRu: 'Увеличение крутизны',
        color: '#f59e0b',
        shifts: [-50, -40, -30, -10, 10, 30, 50, 70, 80, 90, 100, 110, 120, 130, 140, 140, 140, 140, 140],
    },
    {
        id: 'flattener',
        name: 'Flattener',
        nameRu: 'Уплощение кривой',
        color: '#10b981',
        shifts: [50, 40, 30, 10, -10, -30, -50, -70, -80, -90, -100, -110, -120, -130, -140, -140, -140, -140, -140],
    },
    {
        id: 'short_up',
        name: 'Short Rates Up',
        nameRu: 'Рост коротких ставок',
        color: '#8b5cf6',
        shifts: [200, 180, 150, 120, 100, 80, 60, 40, 30, 20, 10, 5, 0, 0, 0, 0, 0, 0, 0],
    },
    {
        id: 'short_down',
        name: 'Short Rates Down',
        nameRu: 'Снижение коротких ставок',
        color: '#ec4899',
        shifts: [-200, -180, -150, -120, -100, -80, -60, -40, -30, -20, -10, -5, 0, 0, 0, 0, 0, 0, 0],
    },
];

// Базовая кривая (текущие ставки, %)
export const BASE_CURVE = [
    16.5, 16.2, 15.8, 15.5, 15.3, 15.0, 14.8, 14.6, 14.5, 14.4, 14.3, 14.2, 14.15, 14.1, 13.9, 13.7, 13.5, 13.3, 13.0
];

// Исторические даты для анализа
export const AVAILABLE_DATES = [
    '2026-01-15',
    '2025-12-15',
    '2025-11-15',
    '2025-10-15',
    '2025-09-15',
    '2025-08-15',
    '2025-07-15',
    '2025-06-15',
    '2025-05-15',
    '2025-04-15',
    '2025-03-15',
    '2025-02-15',
    '2025-01-15',
];

// Генерация данных для исторических дат (имитация)
export function getHistoricalCurve(date: string): number[] {
    const dateIndex = AVAILABLE_DATES.indexOf(date);
    const baseShift = dateIndex * 0.3;
    return BASE_CURVE.map((v, i) => +(v + baseShift + Math.sin(i * 0.5 + dateIndex) * 0.3).toFixed(2));
}
