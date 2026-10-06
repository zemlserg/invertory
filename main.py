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
import jwt
from sqlalchemy import create_engine, Column, String, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

# Компоненты ReportLab для PDF
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas

load_dotenv()

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}?sslmode=disable"

SECRET_KEY = "ASU_TP_MC_SUPER_SECRET_KEY_999"
ALGORITHM = "HS256"

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


class EquipmentHistory(Base):
    __tablename__ = "equipment_history"
    id = Column(String, primary_key=True, index=True)
    equipment_id = Column(String, nullable=False)
    status = Column(String, nullable=False)
    location = Column(String, nullable=True)
    changed_at = Column(DateTime, default=datetime.utcnow)


class User(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    role = Column(String, nullable=False)


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


class LoginRequest(BaseModel):
    username: str
    password: str


def get_current_user(token: str = None, db: Session = Depends(get_db)):
    if not token:
        raise HTTPException(status_code=401, detail="Токен отсутствует")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None: raise HTTPException(status_code=401, detail="Невалидный токен")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Токен истек или поврежден")

    user = db.query(User).filter(User.username == username).first()
    if user is None: raise HTTPException(status_code=401, detail="Пользователь не найден")
    return user


@app.post("/equipment/auth/login")
async def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == req.username).first()
    if not user or user.hashed_password != req.password:
        raise HTTPException(status_code=400, detail="Неверное имя пользователя или пароль")

    expire = datetime.utcnow() + timedelta(hours=12)
    token_data = {"sub": user.username, "exp": expire}
    encoded_jwt = jwt.encode(token_data, SECRET_KEY, algorithm=ALGORITHM)
    return {"access_token": encoded_jwt, "role": user.role, "username": user.username}


@app.post("/equipment", status_code=201)
async def create_equipment(item: EquipmentCreate, db: Session = Depends(get_db),
                           current_user: User = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    generated_id = str(uuid.uuid4())
    qr_url = f"/static/qr/{generated_id}.png"
    db_item = Equipment(
        id=generated_id, name=item.name, serial_number=item.serial_number,
        location=item.location, status=item.status, qr_url=qr_url
    )
    db.add(db_item)
    db_history = EquipmentHistory(
        id=str(uuid.uuid4()), equipment_id=generated_id,
        status=item.status, location=item.location
    )
    db.add(db_history)
    db.commit()
    db.refresh(db_item)
    return db_item


@app.get("/equipment/{equipment_id}")
async def get_equipment(equipment_id: str, db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    item = db.query(Equipment).filter(Equipment.id == equipment_id).first()
    if not item: raise HTTPException(status_code=404, detail="Оборудование не найдено")
    return item


@app.get("/equipment/{equipment_id}/history")
async def get_equipment_history(equipment_id: str, db: Session = Depends(get_db),
                                current_user: User = Depends(get_current_user)):
    return db.query(EquipmentHistory).filter(EquipmentHistory.equipment_id == equipment_id).order_by(
        EquipmentHistory.changed_at.desc()).all()


@app.patch("/equipment/{equipment_id}")
async def update_equipment(equipment_id: str, item: EquipmentUpdate, db: Session = Depends(get_db),
                           current_user: User = Depends(get_current_user)):
    if current_user.role == "viewer": raise HTTPException(status_code=403, detail="Доступ запрещен")
    db_item = db.query(Equipment).filter(Equipment.id == equipment_id).first()
    if not db_item: raise HTTPException(status_code=404, detail="Оборудование не найдено")

    db_item.status = item.status
    if item.location: db_item.location = item.location

    db_history = EquipmentHistory(
        id=str(uuid.uuid4()), equipment_id=equipment_id,
        status=item.status, location=db_item.location
    )
    db.add(db_history)
    db.commit()
    db.refresh(db_item)
    return db_item


@app.get("/", response_class=HTMLResponse)
async def read_index():
    with open("index.html", "r", encoding="utf-8") as f: return f.read()


# === ЖЕЛЕЗОБЕТОННЫЙ ЭНДПОИНТ 5: СТАБИЛЬНЫЙ ВСТРОЕННЫЙ ШРИФТ COURIER ===
@app.get("/equipment/pdf/print")
async def generate_pdf_tags(db: Session = Depends(get_db)):
    items = db.query(Equipment).all()
    if not items: raise HTTPException(status_code=400, detail="В базе данных Amvera пока нет оборудования для печати")

    buffer = io.BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    page_width, page_height = A4
    margin, box_size, gap = 1.5 * cm, 5.0 * cm, 0.4 * cm
    x, y = margin, page_height - margin - box_size

    # Включаем стандартный встроенный Courier-Bold (он железно поддерживает русский язык)
    p.setFont("Courier-Bold", 8)

    for item in items:
        p.setDash(2, 2)
        p.setStrokeColorRGB(0.7, 0.7, 0.7)
        p.rect(x, y, box_size, box_size, stroke=1, fill=0)
        p.setDash()

        # Формируем правильную "Умную ссылку" для сканирования обычной камерой телефона
        smart_url = f"https://invertory-api-uralrus1.amvera.io?id={item.id}"

        qr = qrcode.QRCode(version=1, box_size=10, border=1)
        qr.add_data(smart_url)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white")

        qr_buffer = io.BytesIO()
        qr_img.save(qr_buffer, format="PNG")
        qr_buffer.seek(0)
        p.drawImage(canvas.ImageReader(qr_buffer), x + 0.2 * cm, y + 1.4 * cm, width=3.2 * cm, height=3.2 * cm)

        p.setFillColorRGB(0.1, 0.1, 0.2)
        name_text = f"Nazv: {str(item.name)[:14]}"
        sn_text = f"S/N: {str(item.serial_number)[:14]}" if item.serial_number else "S/N: -"

        p.drawString(x + 0.3 * cm, y + 0.8 * cm, name_text)
        p.drawString(x + 0.3 * cm, y + 0.4 * cm, sn_text)

        x += box_size + gap
        if x + box_size > page_width - margin:
            x = margin
            y -= (box_size + gap)
            if y < margin:
                p.showPage()
                p.setFont("Courier-Bold", 8)
                x = margin
                y = page_height - margin - box_size

    p.showPage()
    p.save()
    buffer.seek(0)
    return StreamingResponse(buffer, media_type="application/pdf",
                             headers={"Content-Disposition": "attachment; filename=qr_labels.pdf"})
