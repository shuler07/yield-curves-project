import { Calendar, ChevronDown } from 'lucide-react';
import { AVAILABLE_DATES } from '../data/mockData';
import '../styles/DateSelector.css';

interface DateSelectorProps {
    selectedDate: string;
    onDateChange: (date: string) => void;
    availableDates?: string[];
}

export default function DateSelector({
                                         selectedDate,
                                         onDateChange,
                                         availableDates = AVAILABLE_DATES,
                                     }: DateSelectorProps) {
    return (
        <div className="date-selector">
            <h3 className="date-selector-header">
                <Calendar className="date-selector-icon" />
                <span className="date-selector-title">Историческая дата</span>
            </h3>
            <div className="date-selector-wrapper">
                <select
                    value={selectedDate}
                    onChange={(e) => onDateChange(e.target.value)}
                    className="date-selector-select"
                >
                    {availableDates.map((date) => (
                        <option key={date} value={date}>
                            {new Date(date).toLocaleDateString('ru-RU', {
                                day: 'numeric',
                                month: 'long',
                                year: 'numeric',
                            })}
                        </option>
                    ))}
                </select>
                <ChevronDown className="date-selector-arrow" />
            </div>
            <p className="date-selector-hint">
                Источник: /curves/processed (MOEX / расчётные кривые).
            </p>
        </div>
    );
}