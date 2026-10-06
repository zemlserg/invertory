import os
import uuid
import io
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
import qrcode
import jwt  # Используется для генерации и проверки зашифрованных JWT-токенов
from sqlalchemy import create_engine, Column, String, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

# Импортируем компоненты ReportLab для генерации PDF-листов А4
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas

# Загружаем переменные окружения (сервер Amvera берет их из вкладки Переменные)
load_dotenv()

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

# Собираем строку подключения к PostgreSQL
DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}?sslmode=disable"

# Секретный ключ, которым бэкенд «подписывает» электронные пропуска (JWT-токены)
SECRET_KEY = "ASU_TP_MC_SUPER_SECRET_KEY_999"
ALGORITHM = "HS256" # Алгоритм хэширования подписи

# Инициализируем движок SQLAlchemy и создаем фабрику сессий для базы данных
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base() # Базовый класс, от которого наследуются все таблицы
# Таблица 1: Основной реестр техники
class Equipment(Base):
    __tablename__ = "equipment"
    id = Column(String, primary_key=True, index=True) # UUID прибора
    name = Column(String, nullable=False)            # Название (ноутбук, монитор и т.д.)
    serial_number = Column(String, nullable=True)    # Серийный номер
    location = Column(String, nullable=True)         # Текущий кабинет / цех
    status = Column(String, nullable=True)           # Статус (В работе, На складе...)
    qr_url = Column(String, nullable=True)           # Ссылка на картинку QR-кода

# Таблица 2: Лог (лента) истории перемещений оборудования
class EquipmentHistory(Base):
    __tablename__ = "equipment_history"
    id = Column(String, primary_key=True, index=True)
    equipment_id = Column(String, nullable=False)    # Связующий UUID прибора
    status = Column(String, nullable=False)           # Какой статус был выставлен
    location = Column(String, nullable=True)         # Какая локация была указана
    changed_at = Column(DateTime, default=datetime.utcnow) # Точное время изменения (по UTC)

# Таблица 3: Реестр сотрудников (пользователей системы)
class User(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False) # Логин (должен быть уникальным)
    hashed_password = Column(String, nullable=False)       # Пароль сотрудника
    role = Column(String, nullable=False)                  # Уровень доступа ('admin', 'engineer', 'viewer')

# Автоматически создаем таблицы в PostgreSQL, если их там еще не было при старте
Base.metadata.create_all(bind=engine)

# Инициализируем само веб-приложение FastAPI
app = FastAPI(title="Система учета оборудования")

# Подключаем CORS-фильтр, чтобы браузеры смартфонов (iOS/Android) могли делать запросы к нашему бэкенду
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Разрешаем запросы со всех адресов в интернете
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Функция-помощник (Dependency Injection) для открытия и автоматического закрытия сессий связи с БД
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close() # Гарантированно закрываем поток, чтобы не перегружать PostgreSQL


# Схемы Pydantic проверяют, чтобы типы данных (строки, числа) были правильными при POST/PATCH запросах
class EquipmentCreate(BaseModel):
    name: str
    serial_number: str = None
    location: str = None
    status: str = "На складе"


class EquipmentUpdate(BaseModel):
    status: str
    location: str = None


class LoginRequest(BaseModel):
    username: str
    password: str


# КРИТИЧЕСКИЙ ENTERPRISE-КОМПОНЕНТ: Проверка электронного пропуска (токена)
def get_current_user(token: str = None, db: Session = Depends(get_db)):
    if not token:
        raise HTTPException(status_code=401, detail="Токен отсутствует. Войдите в систему!")
    try:
        # Расшифровываем токен с помощью нашего секретного ключа SECRET_KEY
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")  # Извлекаем логин владельца токена
        if username is None:
            raise HTTPException(status_code=401, detail="Невалидный токен")
    except jwt.PyJWTError:
        # Если токен подделан или у него истек срок годности (12 часов)
        raise HTTPException(status_code=401, detail="Токен истек или поврежден. Перезайдите!")

    # Ищем сотрудника в таблице users по имени из токена
    user = db.query(User).filter(User.username == username).first()
    if user is None:
        raise HTTPException(status_code=401, detail="Пользователь не найден")
    return user  # Возвращаем объект пользователя со всеми его правами и ролью


# ЭНДПОИНТ ЛОГИНА: Проверяет логин/пароль и выдает JWT-токен
@app.post("/equipment/auth/login")
async def login(req: LoginRequest, db: Session = Depends(get_db)):
    # Ищем пользователя в базе
    user = db.query(User).filter(User.username == req.username).first()
    if not user or user.hashed_password != req.password:
        raise HTTPException(status_code=400, detail="Неверное имя пользователя или пароль")

    # Задаем время жизни электронного ключа — ровно 12 часов
    expire = datetime.utcnow() + timedelta(hours=12)
    token_data = {"sub": user.username, "exp": expire}

    # Шифруем данные в JWT-строку
    encoded_jwt = jwt.encode(token_data, SECRET_KEY, algorithm=ALGORITHM)

    # Возвращаем токен и роль на телефон инженера
    return {"access_token": encoded_jwt, "role": user.role, "username": user.username}


# Создание оборудования (Доступно СТРОГО для роли 'admin')
@app.post("/equipment", status_code=201)
async def create_equipment(item: EquipmentCreate, db: Session = Depends(get_db),
                           current_user: User = Depends(get_current_user)):
    # Серверная проверка прав (Фронтенд обмануть не получится)
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Доступ запрещен: Создавать технику может только Администратор")

    generated_id = str(uuid.uuid4())  # Генерируем уникальный 36-значный UUID
    qr_url = f"/static/qr/{generated_id}.png"

    # Создаем запись в таблице equipment
    db_item = Equipment(
        id=generated_id, name=item.name, serial_number=item.serial_number,
        location=item.location, status=item.status, qr_url=qr_url
    )
    db.add(db_item)

    # Сразу же пишем первое событие в историю перемещений
    db_history = EquipmentHistory(
        id=str(uuid.uuid4()), equipment_id=generated_id, status=item.status, location=item.location
    )
    db.add(db_history)
    db.commit()
    db.refresh(db_item)
    return db_item


# Получение информации о конкретном приборе по его ID
@app.get("/equipment/{equipment_id}")
async def get_equipment(equipment_id: str, db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    item = db.query(Equipment).filter(Equipment.id == equipment_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Оборудование не найдено")
    return item


# Получение ленты логов истории для конкретного прибора
@app.get("/equipment/{equipment_id}/history")
async def get_equipment_history(equipment_id: str, db: Session = Depends(get_db),
                                current_user: User = Depends(get_current_user)):
    return db.query(EquipmentHistory).filter(EquipmentHistory.equipment_id == equipment_id).order_by(
        EquipmentHistory.changed_at.desc()).all()


# Редактирование локации/статуса (Запрещено для роли 'viewer')
@app.patch("/equipment/{equipment_id}")
async def update_equipment(equipment_id: str, item: EquipmentUpdate, db: Session = Depends(get_db),
                           current_user: User = Depends(get_current_user)):
    if current_user.role == "viewer":
        raise HTTPException(status_code=403, detail="Доступ запрещен: Наблюдатель не может изменять данные")

    db_item = db.query(Equipment).filter(Equipment.id == equipment_id).first()
    if not db_item:
        raise HTTPException(status_code=404, detail="Оборудование не найдено")

    # Обновляем поля
    db_item.status = item.status
    if item.location:
        db_item.location = item.location

    # АВТОЛОГИРОВАНИЕ: добавляем новую строчку в историю перемещений
    db_history = EquipmentHistory(
        id=str(uuid.uuid4()), equipment_id=equipment_id, status=item.status, location=db_item.location
    )
    db.add(db_history)
    db.commit()
    db.refresh(db_item)
    return db_item


# Раздача главного файла интерфейса index.html на экраны смартфонов
@app.get("/", response_class=HTMLResponse)
async def read_index():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()


# МОДУЛЬ ENTERPRISE-ПЕЧАТИ: Создание PDF-листа с наклейками 5х5 см
@app.get("/equipment/pdf/print")
async def generate_pdf_tags(db: Session = Depends(get_db)):
    # Забираем из PostgreSQL абсолютно всю технику
    items = db.query(Equipment).all()
    if not items:
        raise HTTPException(status_code=400, detail="В базе данных Amvera пока нет оборудования для печати")

    # Создаем виртуальный буфер в оперативной памяти (RAM) сервера, чтобы не засорять жесткий диск
    buffer = io.BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)

    page_width, page_height = A4
    margin, box_size, gap = 1.5 * cm, 5.0 * cm, 0.4 * cm  # Геометрия сетки наклеек
    x, y = margin, page_height - margin - box_size

    # Включаем стандартный встроенный шрифт Courier-Bold для безупречной поддержки кириллицы
    p.setFont("Courier-Bold", 8)

    for item in items:
        # Рисуем светло-серую пунктирную рамку наклейки для разрезания ножницами
        p.setDash(2, 2)
        p.setStrokeColorRGB(0.7, 0.7, 0.7)
        p.rect(x, y, box_size, box_size, stroke=1, fill=0)
        p.setDash()  # Сбрасываем пунктир обратно

        # Формируем кристально чистую "Умную ссылку" для мгновенного распознавания камерой iPhone
        smart_url = f"https://amvera.io{str(item.id).strip()}"

        # Инициализируем генератор QR-кода с зоной безопасности (border=2), чтобы пиксели не слипались с буквами
        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=2)
        qr.add_data(smart_url)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white")

        # Сохраняем картинку QR-кода во временные байты в памяти
        qr_buffer = io.BytesIO()
        qr_img.save(qr_buffer, format="PNG")
        qr_buffer.seek(0)

        # Рисуем QR-код по строго выверенным координатам (смещен вверх, размер 2.9х2.9 см)
        p.drawImage(canvas.ImageReader(qr_buffer), x + 1.05 * cm, y + 1.6 * cm, width=2.9 * cm, height=2.9 * cm)

        # Печатаем текстовую информацию в самом низу наклейки (полностью изолируя от QR)
        p.setFillColorRGB(0.1, 0.1, 0.2)
        name_text = f"Nazv: {str(item.name)[:14]}"
        sn_text = f"S/N: {str(item.serial_number)[:14]}" if item.serial_number else "S/N: -"

        p.drawString(x + 0.4 * cm, y + 0.8 * cm, name_text)
        p.drawString(x + 0.4 * cm, y + 0.4 * cm, sn_text)

        # Смещаем координату X вправо для прорисовки следующей наклейки в ряду
        x += box_size + gap

        # Если следующая наклейка не помещается по ширине А4 — переходим на новую строку (шаг вниз по Y)
        if x + box_size > page_width - margin:
            x = margin
            y -= (box_size + gap)

            # Если дошли до самого низа листа — автоматически создаем новую чистую страницу А4
            if y < margin:
                p.showPage()
                p.setFont("Courier-Bold", 8)  # Заново закрепляем шрифт кириллицы на новом листе
                x = margin
                y = page_height - margin - box_size

    p.showPage()
    p.save()  # Завершаем сборку PDF-документа
    buffer.seek(0)

    # Отправляем готовый PDF-файл из оперативной памяти прямо в загрузки браузера
    return StreamingResponse(buffer, media_type="application/pdf",
                             headers={"Content-Disposition": "attachment; filename=qr_labels.pdf"})
