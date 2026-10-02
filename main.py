import os
import uuid
import io
from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
import qrcode
from sqlalchemy import create_engine, Column, String
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

# Подключаем компоненты ReportLab для генерации PDF
from reportlab.lib.pagesizes import letter, A4
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

load_dotenv()

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}?sslmode=disable"

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Equipment(Base):
    __tablename__ = "equipment"
    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False)
    serial_number = Column(String, nullable=True)
    location = Column(String, nullable=True)
    status = Column(String, nullable=True)
    qr_url = Column(String, nullable=True)


Base.metadata.create_all(bind=engine)
app = FastAPI(title="Система учета оборудования")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class EquipmentCreate(BaseModel):
    name: str
    serial_number: str = None
    location: str = None
    status: str = "На складе"


class EquipmentUpdate(BaseModel):
    status: str
    location: str = None


@app.post("/equipment", status_code=201)
async def create_equipment(item: EquipmentCreate, db: Session = Depends(get_db)):
    generated_id = str(uuid.uuid4())
    qr_url = f"/static/qr/{generated_id}.png"
    db_item = Equipment(
        id=generated_id, name=item.name, serial_number=item.serial_number,
        location=item.location, status=item.status, qr_url=qr_url
    )
    db.add(db_item)
    db.commit()
    db.refresh(db_item)
    return db_item


@app.get("/equipment/{equipment_id}")
async def get_equipment(equipment_id: str, db: Session = Depends(get_db)):
    item = db.query(Equipment).filter(Equipment.id == equipment_id).first()
    if not item: raise HTTPException(status_code=404, detail="Оборудование не найдено")
    return item


@app.patch("/equipment/{equipment_id}")
async def update_equipment(equipment_id: str, item: EquipmentUpdate, db: Session = Depends(get_db)):
    db_item = db.query(Equipment).filter(Equipment.id == equipment_id).first()
    if not db_item: raise HTTPException(status_code=404, detail="Оборудование не найдено")
    db_item.status = item.status
    if item.location: db_item.location = item.location
    db.commit()
    db.refresh(db_item)
    return db_item


@app.get("/", response_class=HTMLResponse)
async def read_index():
    with open("index.html", "r", encoding="utf-8") as f: return f.read()


# === НОВЫЙ ЭНДПОИНТ 5: ГЕНЕРАЦИЯ PDF С НАКЛЕЙКАМИ 5х5 см ДЛЯ ПЕЧАТИ ===
@app.get("/equipment/pdf/print")
async def generate_pdf_tags(db: Session = Depends(get_db)):
    items = db.query(Equipment).all()
    if not items:
        raise HTTPException(status_code=400, detail="В базе данных Amvera пока нет оборудования для печати")

    # Создаем виртуальный буфер в оперативной памяти для сборки PDF
    buffer = io.BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)

    # Геометрия листа А4 и сетки наклеек
    page_width, page_height = A4
    margin = 1.5 * cm  # Отступы от краев листа
    box_size = 5.0 * cm  # Фиксированный размер наклейки 5х5 см
    gap = 0.4 * cm  # Зазор между наклейками, чтобы удобно резать

    x = margin
    y = page_height - margin - box_size

    # Регистрация стандартного шрифта, который всегда есть в системе ReportLab
    # Используем Helvetica для аккуратного английского и цифр
    p.setFont("Helvetica-Bold", 8)

    for item in items:
        # 1. Рисуем пунктирную границу для вырезания ножницами
        p.setDash(2, 2)
        p.setStrokeColorRGB(0.7, 0.7, 0.7)  # Светло-серый цвет линий
        p.rect(x, y, box_size, box_size, stroke=1, fill=0)
        p.setDash()  # Сбрасываем пунктир для обычных элементов

        # 2. Генерируем "Умную ссылку" для этого предмета
        # Бэкенд автоматически подставит текущий облачный домен
        smart_url = f"https://invertory-api-uralrus1.amvera.io/?id={item.id}"

        # 3. Создаем изображение QR-кода в памяти
        qr = qrcode.QRCode(version=1, box_size=10, border=1)
        qr.add_data(smart_url)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white")

        # Конвертируем QR-код в байты, чтобы ReportLab мог его нарисовать
        qr_buffer = io.BytesIO()
        qr_img.save(qr_buffer, format="PNG")
        qr_buffer.seek(0)

        # Рисуем QR-код внутри наклейки (размер 3.2х3.2 см, центрирован по вертикали)
        p.drawImage(canvas.ImageReader(qr_buffer), x + 0.2 * cm, y + 1.4 * cm, width=3.2 * cm, height=3.2 * cm)

        # 4. Пишем текстовую информацию на наклейке
        p.setFillColorRGB(0.1, 0.1, 0.2)

        # Обрезаем слишком длинные названия, чтобы они не вылезали за наклейку
        name_text = f"Name: {item.name[:18]}"
        sn_text = f"S/N: {item.serial_number[:18]}" if item.serial_number else "S/N: -"

        p.drawString(x + 0.3 * cm, y + 0.8 * cm, name_text)
        p.drawString(x + 0.3 * cm, y + 0.4 * cm, sn_text)

        # Расчет координат для следующей наклейки (шаг вправо)
        x += box_size + gap

        # Если следующая наклейка не помещается по ширине листа — переходим на новую строку
        if x + box_size > page_width - margin:
            x = margin
            y -= (box_size + gap)  # Шаг вниз

            # Если дошли до низа листа — создаем новую страницу А4
            if y < margin:
                p.showPage()
                p.setFont("Helvetica-Bold", 8)
                x = margin
                y = page_height - margin - box_size

    p.showPage()
    p.save()
    buffer.seek(0)

    # Отдаем файл в браузер как скачиваемый поток PDF
    return StreamingResponse(buffer, media_type="application/pdf",
                             headers={"Content-Disposition": "attachment; filename=qr_labels.pdf"})
