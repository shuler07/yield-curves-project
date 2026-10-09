import pandas as pd
import numpy as np

def generate_irrbb_history(input_file="merged_zcyc_ranges.csv", output_file="IRRBB_Scenarios_History.xlsx"):
    print("Загрузка исторических данных...")
    # Читаем исходный файл, разделитель точка с запятой
    df = pd.read_csv(input_file, sep=";")
    
    # Извлекаем сроки (tenors) из названий колонок (например, 'period_0.25' -> 0.25)
    period_cols = [col for col in df.columns if col.startswith('period_')]
    tenors = np.array([float(col.split('_')[1]) for col in period_cols])
    
    # Очищаем данные: заменяем запятые на точки и конвертируем в числа (float)
    for col in period_cols:
        df[col] = df[col].astype(str).str.replace(',', '.').astype(float)
        
    # Параметры шока Базеля
    shock = 4.0  # Базовый шок 400 б.п. (4.0%)
    S_t = np.exp(-tenors / 4.0)  # Функция экспоненциального затухания
    
    # Словарь для хранения датафреймов (ключ - название листа в Excel)
    scenarios = {'Baseline': df.copy()}
    
    # Вспомогательная функция для применения математики ко всей матрице сразу
    def apply_scenario(calc_func):
        temp_df = df.copy()
        # Применяем формулу сразу ко всем строкам и колонкам со ставками
        temp_df[period_cols] = calc_func(df[period_cols].values)
        return temp_df

    print("Расчет 6 базельских сценариев для всей истории...")
    scenarios['Parallel_Up']   = apply_scenario(lambda base: base + shock)
    
    # Для сценариев падения используем np.maximum, чтобы ставки не падали ниже 0% (floor)
    scenarios['Parallel_Down'] = apply_scenario(lambda base: np.maximum(base - shock, 0))
    scenarios['Short_Up']      = apply_scenario(lambda base: base + shock * S_t)
    scenarios['Short_Down']    = apply_scenario(lambda base: np.maximum(base - shock * S_t, 0))
    scenarios['Steepener']     = apply_scenario(lambda base: base - 0.65 * shock * S_t + 0.9 * shock * (1 - S_t))
    scenarios['Flattener']     = apply_scenario(lambda base: base + 0.8 * shock * S_t - 0.6 * shock * (1 - S_t))
    
    print(f"Экспорт данных в {output_file}...")
    # Сохраняем в Excel (потребуется установленная библиотека openpyxl: pip install openpyxl)
    with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
        for sheet_name, data in scenarios.items():
            # Округляем до 2 знаков после запятой для красоты
            data.update(data[period_cols].round(2))
            data.to_excel(writer, sheet_name=sheet_name, index=False)
            
    print("Готово! Excel-файл успешно сформирован.")

if __name__ == "__main__":
    generate_irrbb_history()