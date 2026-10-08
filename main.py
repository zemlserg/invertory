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
import jwt  # Библиотека для шифрования электронных пропусков (JWT-токенов)
from sqlalchemy import create_engine, Column, String, DateTime, Integer
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

# Импортируем компоненты ReportLab для сборки печатных PDF-форм
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# Загружаем переменные окружения (хост, логин, пароль базы данных)
load_dotenv()

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

# Собираем строку подключения к PostgreSQL Amvera
DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}?sslmode=disable"

# Секретный ключ шифрования для защиты сессий пользователей
SECRET_KEY = "ASU_TP_MC_SUPER_SECRET_KEY_999"
ALGORITHM = "HS256"

# Запускаем движок СУБД и создаем фабрику изолированных сессий
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ОПТИМИЗИРОВАННАЯ ТАБЛИЦА ОБОРУДОВАНИЯ ПО ВЕДОМОСТИ ОСТАТКОВ
class Equipment(Base):
    __tablename__ = "equipment"

    # Номенклатурный номер из вашей таблицы становится главным текстовым ID
    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False)  # Название компонента АСУ
    location = Column(String, nullable=True)  # Текущее место установки
    status = Column(String, nullable=True)  # Статус (На складе, В работе...)
    quantity = Column(Integer, default=0)  # ИЗМЕНЯЕМОЕ КОЛИЧЕСТВО (Остаток, шт)

    # Пустые колонки "про запас" для будущего расширения ведомости
    spare_column_1 = Column(String, nullable=True)
    spare_column_2 = Column(String, nullable=True)
    spare_column_3 = Column(String, nullable=True)


# Таблица истории перемещений (теперь логирует и движение остатков количества)
class EquipmentHistory(Base):
    __tablename__ = "equipment_history"
    id = Column(String, primary_key=True, index=True)
    equipment_id = Column(String, nullable=False)  # Номенклатурный номер прибора
    status = Column(String, nullable=False)  # Записанный статус
    location = Column(String, nullable=True)  # Записанная локация
    quantity = Column(Integer, nullable=True)  # Записанный остаток количества
    changed_at = Column(DateTime, default=datetime.utcnow)  # Время логирования


class User(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    role = Column(String, nullable=False)


# Принудительно генерируем структуру таблиц в PostgreSQL
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Система учета оборудования")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Диспетчер автоматического открытия и закрытия сессий связи с PostgreSQL
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Схемы валидации данных Pydantic (Количество объявлено как изменяемое)
class EquipmentCreate(BaseModel):
    id: str  # Вводится вручную (Номенклатурный номер)
    name: str
    location: str = None
    status: str = "На складе"
    quantity: int = 0


class EquipmentUpdate(BaseModel):
    status: str
    location: str = None
    quantity: int = 0  # Количество теперь можно изменять через PATCH


class LoginRequest(BaseModel):
    username: str
    password: str


# МОДУЛЬ ЗАЩИТЫ БЭКЕНДА: Расшифровывает JWT-токен и проверяет роль сотрудника
def get_current_user(token: str = None, db: Session = Depends(get_db)):
    if not token:
        raise HTTPException(status_code=401, detail="Токен отсутствует. Авторизуйтесь!")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None: raise HTTPException(status_code=401, detail="Невалидный токен")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Сессия безопасности истекла. Перезайдите!")

    user = db.query(User).filter(User.username == username).first()
    if user is None: raise HTTPException(status_code=401, detail="Сотрудник не найден")
    return user


# ЭНДПОИНТ АВТОРИЗАЦИИ: Выдает JWT-токен на 12 часов
@app.post("/equipment/auth/login")
async def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == req.username).first()
    if not user or user.hashed_password != req.password:
        raise HTTPException(status_code=400, detail="Неверное имя пользователя или пароль")

    expire = datetime.utcnow() + timedelta(hours=12)
    token_data = {"sub": user.username, "exp": expire}
    encoded_jwt = jwt.encode(token_data, SECRET_KEY, algorithm=ALGORITHM)
    return {"access_token": encoded_jwt, "role": user.role, "username": user.username}


# Создание новой позиции (Разрешено только Администратору)
@app.post("/equipment", status_code=201)
async def create_equipment(item: EquipmentCreate, db: Session = Depends(get_db),
                           current_user: User = Depends(get_current_user)):
    if current_user.role != "admin": raise HTTPException(status_code=403, detail="Доступ запрещен")

    existing = db.query(Equipment).filter(Equipment.id == item.id).first()
    if existing: raise HTTPException(status_code=400, detail="Позиция с таким номенклатурным номером уже есть")

    # Чистое создание записи по ведомости без старого поля qr_url
    db_item = Equipment(id=item.id, name=item.name, location=item.location, status=item.status, quantity=item.quantity)
    db.add(db_item)

    # Пишем начальную точку в историю изменений
    db_history = EquipmentHistory(id=str(uuid.uuid4()), equipment_id=item.id, status=item.status,
                                  location=item.location, quantity=item.quantity)
    db.add(db_history)
    db.commit()
    db.refresh(db_item)
    return db_item


# Поиск карточки по номенклатурному номеру
@app.get("/equipment/{equipment_id}")
async def get_equipment(equipment_id: str, db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    item = db.query(Equipment).filter(Equipment.id == equipment_id).first()
    if not item: raise HTTPException(status_code=404, detail="Позиция не найдена в ведомости")
    return item


# Выгрузка логов истории для конкретного номера устройства
@app.get("/equipment/{equipment_id}/history")
async def get_equipment_history(equipment_id: str, db: Session = Depends(get_db),
                                current_user: User = Depends(get_current_user)):
    return db.query(EquipmentHistory).filter(EquipmentHistory.equipment_id == equipment_id).order_by(
        EquipmentHistory.changed_at.desc()).all()


# СОХРАНЕНИЕ ИЗМЕНЯЕМЫХ ПАРАМЕТРОВ: Перезапись статуса, локации и количества (Остатка)
@app.patch("/equipment/{equipment_id}")
async def update_equipment(equipment_id: str, item: EquipmentUpdate, db: Session = Depends(get_db),
                           current_user: User = Depends(get_current_user)):
    if current_user.role == "viewer": raise HTTPException(status_code=403,
                                                          detail="Наблюдатель не может изменять данные")
    db_item = db.query(Equipment).filter(Equipment.id == equipment_id).first()
    if not db_item: raise HTTPException(status_code=404, detail="Позиция не найдена")

    # Применяем новые изменяемые параметры из карточки смартфона
    db_item.status = item.status
    db_item.quantity = item.quantity  # Фиксируем новый остаток количества
    if item.location: db_item.location = item.location

    # Автологирование: фиксируем новые остатки и место в истории
    db_history = EquipmentHistory(id=str(uuid.uuid4()), equipment_id=equipment_id, status=item.status,
                                  location=db_item.location, quantity=db_item.quantity)
    db.add(db_history)
    db.commit()
    db.refresh(db_item)
    return db_item


# Отдача интерфейса сайта прямо на экраны телефонов
@app.get("/", response_class=HTMLResponse)
async def read_index():
    with open("index.html", "r", encoding="utf-8") as f: return f.read()


# МОДУЛЬ ГЕНЕРАЦИИ НАКЛЕЕК: Сборка PDF на лету в оперативной памяти (RAM)
@app.get("/equipment/pdf/print")
async def generate_pdf_tags(db: Session = Depends(get_db)):
    import urllib.request
    items = db.query(Equipment).all()
    if not items: raise HTTPException(status_code=400, detail="База данных ведомости пока пуста")

    # Активируем стабильный шрифт для кириллицы
    font_path = "DejaVuSans-Bold.ttf"
    if not os.path.exists(font_path):
        try:
            font_url = "https://github.com"
            urllib.request.urlretrieve(font_url, font_path)
        except:
            pass
    try:
        pdfmetrics.registerFont(TTFont('DejaVuSans-Bold', font_path))
        font_name = "DejaVuSans-Bold"
    except:
        font_name = "Courier-Bold"  # Железный резервный шрифт, если Linux заблокирует внешние файлы

    buffer = io.BytesIO()  # Виртуальный буфер в RAM
    p = canvas.Canvas(buffer, pagesize=A4)
    page_width, page_height = A4
    margin, box_size, gap = 1.5 * cm, 5.0 * cm, 0.4 * cm
    x, y = margin, page_height - margin - box_size
    p.setFont(font_name, 8)

    for item in items:
        # Пунктирная разметка для вырезания
        p.setDash(2, 2)
        p.setStrokeColorRGB(0.7, 0.7, 0.7)
        p.rect(x, y, box_size, box_size, stroke=1, fill=0)
        p.setDash()

        # Зашиваем Умную ссылку с номенклатурным номером в QR-код
        smart_url = f"https://invertory-api-uralrus1.amvera.io/?id={str(item.id).strip()}"

        qr = qrcode.QRCode(version=1, box_size=10, border=1)
        qr.add_data(smart_url)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white")

        qr_buffer = io.BytesIO()
        qr_img.save(qr_buffer, format="PNG")
        qr_buffer.seek(0)
        p.drawImage(canvas.ImageReader(qr_buffer), x + 0.2 * cm, y + 1.4 * cm, width=3.2 * cm, height=3.2 * cm)

        # Печать сопроводительного текста на русском языке на самой наклейке
        p.setFillColorRGB(0.1, 0.1, 0.2)
        name_text = f"Код: {str(item.id)}"
        sn_text = f"Кол-во: {str(item.quantity)} шт."
        p.drawString(x + 0.3 * cm, y + 0.8 * cm, name_text)
        p.drawString(x + 0.3 * cm, y + 0.4 * cm, sn_text)

        x += box_size + gap
        if x + box_size > page_width - margin:
            x = margin
            y -= (box_size + gap)
            if y < margin:
                p.showPage()
                p.setFont(font_name, 8)
                x = margin
                y = page_height - margin - box_size

    p.showPage()
    p.save()
    buffer.seek(0)
    # Выгружаем собранный PDF-поток прямо в загрузки браузера
    return StreamingResponse(buffer, media_type="application/pdf",
                             headers={"Content-Disposition": "attachment; filename=qr_labels.pdf"})
