# Миграции БД

Применить все ревизии из корня проекта, используя `DATABASE_URL` из окружения
или `.env`:

```powershell
python -m alembic upgrade head
python -m alembic current
python -m alembic check
```

Ревизия `0002_curve_point_flags` добавляет в `curve_points`:

| Поле | Тип | Начальное значение | Значение |
| --- | --- | --- | --- |
| `is_interpolated` | `BOOLEAN NOT NULL` | `false` | Ставка получена интерполяцией |
| `is_extrapolated` | `BOOLEAN NOT NULL` | `false` | Ставка получена экстраполяцией |
| `is_deleted` | `BOOLEAN NOT NULL` | `false` | Точка помечена удалённой |

Существующие строки получают `false` во всех трёх полях. Для новых строк
значения по умолчанию заданы и в PostgreSQL, и в ORM. Флаг удаления сам по себе
не удаляет строку; фильтрацию по нему задаёт код чтения данных.

Исходная ревизия `0001_create_db` не изменяется. Откат новой ревизии в тестовой БД:

```powershell
python -m alembic downgrade 0001_create_db
```

Откат удаляет только три новых столбца и их значения, сохраняя остальные данные.
