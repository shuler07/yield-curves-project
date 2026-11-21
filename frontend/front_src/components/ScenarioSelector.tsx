import { STRESS_SCENARIOS, type StressScenario } from '../data/mockData';
import '../styles/ScenarioSelector.css';

export interface ScenarioSelectorProps {
    activeScenarios: string[];
    scenarios?: StressScenario[];
    onToggle: (id: string) => void;
    onClearAll: () => void;
    onSelectAll: () => void;
}

export default function ScenarioSelector({
                                             activeScenarios,
                                             scenarios = STRESS_SCENARIOS,
                                             onToggle,
                                             onClearAll,
                                             onSelectAll,
                                         }: ScenarioSelectorProps) {
    return (
        <div className="scenario-selector">
            <div className="scenario-selector-header">
                <h3 className="scenario-selector-title">Стресс-сценарии</h3>
                <div className="scenario-selector-actions">
                    <button onClick={onSelectAll} className="scenario-action-btn">
                        Все
                    </button>
                    <button onClick={onClearAll} className="scenario-action-btn">
                        Сбросить
                    </button>
                </div>
            </div>
            <div className="scenario-grid">
                {scenarios.map((scenario) => {
                    const isActive = activeScenarios.includes(scenario.id);
                    return (
                        <button
                            key={scenario.id}
                            onClick={() => onToggle(scenario.id)}
                            className={`scenario-btn ${isActive ? 'active' : ''}`}
                            style={{
                                borderColor: isActive ? scenario.color : undefined,
                                backgroundColor: isActive ? `${scenario.color}15` : undefined,
                            }}
                        >
                            <div
                                className="scenario-color-dot"
                                style={{ backgroundColor: scenario.color }}
                            />
                            <div className="scenario-info">
                                <div className="scenario-name">{scenario.nameRu}</div>
                                <div className="scenario-name-en">{scenario.name}</div>
                            </div>
                        </button>
                    );
                })}
            </div>
        </div>
    );
}