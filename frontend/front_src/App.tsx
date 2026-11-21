import { useState } from 'react';
import { BarChart3, Activity, TrendingUp } from 'lucide-react';
import Dashboard from './components/Dashboard';
import './styles/App.css';

type Tab = 'dashboard'

function App() {
    const [activeTab, setActiveTab] = useState<Tab>('dashboard');

    return (
        <div className="app">
            {/* Navigation */}
            <nav className="nav">
                <div className="nav-container">
                    <div className="nav-content">
                        <div className="nav-brand">
                            <div className="nav-logo">
                                <TrendingUp className="nav-logo-icon" />
                            </div>
                            <div>
                                <h1 className="nav-title">IRRBB Monitor</h1>
                                <p className="nav-subtitle">Interest Rate Risk in the Banking Book</p>
                            </div>
                        </div>
                        <div className="nav-tabs">
                            <button
                                onClick={() => setActiveTab('dashboard')}
                                className={`nav-tab ${activeTab === 'dashboard' ? 'active' : ''}`}
                            >
                                <BarChart3 className="nav-tab-icon" />
                                <span className="nav-tab-text">Дашборд</span>
                            </button>
                        </div>
                        <div className="nav-status">
                            <Activity className="nav-status-icon" />
                            <span className="nav-status-text">Online</span>
                        </div>
                    </div>
                </div>
            </nav>

            <main className="main">
                {activeTab === 'dashboard' && <Dashboard />}
            </main>

            {/* Footer */}
            <footer className="footer">
                <div className="footer-container">
                    <div className="footer-content">
                        <span>IRRBB Monitor v1.0 • Базель III/IV • 19 бакетов • 6 стресс-сценариев</span>
                        <span>Данные: MOEX ISS API • Обновлено: 15.01.2026</span>
                    </div>
                </div>
            </footer>
        </div>
    );
}

export default App;
