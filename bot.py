"""
Telegram Quiz Bot
=================
يحوّل الأسئلة النصية أو ملفات PDF/Word لـ Polls على تيليجرام.

المتطلبات:
    pip install pyTelegramBotAPI pdfplumber python-docx

حدود تيليجرام للـ Poll:
    - السؤال:    300 حرف كحد أقصى
    - كل اختيار: 100 حرف كحد أقصى
    - الشرح:     200 حرف كحد أقصى
    - عدد الاختيارات: 2 - 10
"""

import logging
import os
import re
import tempfile
import time

import telebot  # pip install pyTelegramBotAPI
from telebot.apihelper import ApiTelegramException

# ─────────────────────────────────────────────
# الإعدادات
# ─────────────────────────────────────────────

TOKEN = "8708642161:AAHfK6ufA_OHEbAPxA07PqZhz926rL3x100"

# حدود تيليجرام الرسمية
TELEGRAM_QUESTION_LIMIT    = 300
TELEGRAM_OPTION_LIMIT      = 100
TELEGRAM_EXPLANATION_LIMIT = 200
TELEGRAM_MAX_OPTIONS       = 10
TELEGRAM_MIN_OPTIONS       = 2

# الحد الأقصى لحجم الملف (20 MB = حد تيليجرام للبوتات)
MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024

# الانتظار بين كل poll لتجنب حظر تيليجرام
POLL_SEND_DELAY = 0.4

# ─────────────────────────────────────────────
# السجل (Logging)
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# تهيئة البوت
# ─────────────────────────────────────────────

bot = telebot.TeleBot(TOKEN, parse_mode=None)


# ─────────────────────────────────────────────
# استخراج النص من الملفات
# ─────────────────────────────────────────────

def extract_text_from_pdf(file_path: str) -> str:
    """استخراج النص من ملف PDF باستخدام pdfplumber."""
    try:
        import pdfplumber
    except ImportError:
        logger.error("مكتبة pdfplumber غير مثبتة. شغّل: pip install pdfplumber")
        return ""

    text_parts = []
    try:
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
    except Exception as e:
        logger.error(f"خطأ أثناء قراءة PDF: {e}")
        return ""

    return "\n".join(text_parts)


def extract_text_from_docx(file_path: str) -> str:
    """استخراج النص من ملف Word (.docx) باستخدام python-docx."""
    try:
        from docx import Document
    except ImportError:
        logger.error("مكتبة python-docx غير مثبتة. شغّل: pip install python-docx")
        return ""

    try:
        doc = Document(file_path)
        lines = [para.text for para in doc.paragraphs if para.text.strip()]
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"خطأ أثناء قراءة Word: {e}")
        return ""


# ─────────────────────────────────────────────
# تنظيف النص
# ─────────────────────────────────────────────

def clean_text(text: str) -> str:
    """إزالة الأحرف غير المرئية والـ null bytes من النص."""
    text = text.replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # إزالة أسطر فارغة متكررة (أكثر من 2)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ─────────────────────────────────────────────
# محلّل الأسئلة
# ─────────────────────────────────────────────

def truncate(text: str, limit: int) -> str:
    """قص النص إذا تجاوز الحد مع إضافة '...' للتنبيه."""
    if len(text) <= limit:
        return text
    return text[: limit - 3].strip() + "..."


def parse_questions(text: str):
    """
    تحليل النص واستخراج الأسئلة.

    الصيغة المطلوبة:
        1. السؤال
        A. اختيار
        B. اختيار *     <- الإجابة الصحيحة
        C. اختيار
        D. اختيار
        ? الشرح (اختياري)

    الإرجاع:
        valid_questions  : list[(question, options, correct_index, explanation)]
        invalid_blocks   : list[(block_text, reject_reason)]
    """
    valid_questions = []
    invalid_blocks  = []

    # تقسيم النص عند بداية كل سؤال (رقم + نقطة + محتوى)
    blocks = re.split(r"(?:^|\n)(?=\d+\.\s*\S)", text.strip(), flags=re.MULTILINE)

    for block in blocks:
        if not block.strip():
            continue

        lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]

        # تجاهل البلوكات التي لا تبدأ برقم
        if not lines or not re.match(r"^\d+\.", lines[0]):
            continue

        question     = None
        options      = []
        correct      = None
        explanation  = ""
        reject_reason = ""

        for line in lines:
            # السؤال: رقم + نقطة + نص
            if re.match(r"^\d+\.", line) and question is None:
                parts  = line.split(".", 1)
                q_text = parts[1].strip() if len(parts) > 1 else ""
                if q_text:
                    question = f"{parts[0].strip()}. {q_text}"

            # الاختيارات: حرف + نقطة + مسافة + نص
            elif re.match(r"^[A-Za-z]\.\s+\S", line):
                option = line[2:].strip()
                if "*" in option:
                    option  = option.replace("*", "").strip()
                    correct = len(options)
                if option:
                    options.append(option)

            # الشرح
            elif line.startswith("?"):
                explanation = line[1:].strip()

        # ─── التحقق من صحة السؤال ───
        if not question:
            reject_reason = "لا يوجد نص للسؤال"
        elif len(options) < TELEGRAM_MIN_OPTIONS:
            reject_reason = f"عدد الاختيارات أقل من {TELEGRAM_MIN_OPTIONS}"
        elif len(options) > TELEGRAM_MAX_OPTIONS:
            reject_reason = f"عدد الاختيارات ({len(options)}) أكثر من {TELEGRAM_MAX_OPTIONS}"
        elif correct is None:
            reject_reason = "لا توجد إجابة صحيحة — ضع * بجانب الإجابة الصحيحة"

        if reject_reason:
            invalid_blocks.append((block.strip(), reject_reason))
            continue

        # ─── تطبيق حدود تيليجرام ───
        question    = truncate(question,    TELEGRAM_QUESTION_LIMIT)
        options     = [truncate(opt, TELEGRAM_OPTION_LIMIT) for opt in options]
        explanation = truncate(explanation, TELEGRAM_EXPLANATION_LIMIT)

        valid_questions.append((question, options, correct, explanation))

    return valid_questions, invalid_blocks


# ─────────────────────────────────────────────
# إرسال الـ Polls
# ─────────────────────────────────────────────

def send_poll_with_retry(chat_id: int, question: str, options: list,
                         correct: int, explanation: str, retries: int = 3) -> bool:
    """إرسال poll مع إعادة المحاولة عند تجاوز الـ Rate Limit."""
    for attempt in range(1, retries + 1):
        try:
            bot.send_poll(
                chat_id,
                question,
                options,
                type="quiz",
                correct_option_id=correct,
                # تيليجرام يرفض explanation = "" لذا نحوّله لـ None
                explanation=explanation if explanation else None,
                is_anonymous=True,
            )
            return True

        except ApiTelegramException as e:
            if e.error_code == 429:                    # Too Many Requests
                retry_after = int(
                    (e.result_json or {})
                    .get("parameters", {})
                    .get("retry_after", 5)
                )
                logger.warning(f"Rate limit — انتظار {retry_after}s (محاولة {attempt}/{retries})")
                time.sleep(retry_after)
            else:
                logger.error(f"خطأ تيليجرام: [{e.error_code}] {e.description}")
                return False

        except Exception as e:
            logger.error(f"خطأ غير متوقع في send_poll: {e}")
            return False

    logger.error("استُنفدت محاولات إعادة الإرسال.")
    return False


def process_and_send(chat_id: int, text: str):
    """تحليل النص وإرسال الـ Polls."""
    text = clean_text(text)

    if not text:
        bot.send_message(chat_id, "❌ النص فارغ أو لا يمكن قراءته.")
        return

    valid_questions, invalid_blocks = parse_questions(text)

    if not valid_questions and not invalid_blocks:
        bot.send_message(
            chat_id,
            "❌ لم يتم العثور على أي أسئلة.\n\n"
            "تأكد أن الأسئلة مكتوبة بالصيغة الصحيحة.\n"
            "أرسل /help لعرض الصيغة.",
        )
        return

    # ─── إرسال الأسئلة السليمة ───
    if valid_questions:
        bot.send_message(
            chat_id,
            f"✅ تم العثور على {len(valid_questions)} سؤال. جاري الإرسال...",
        )
        sent = failed = 0
        for question, options, correct, explanation in valid_questions:
            if send_poll_with_retry(chat_id, question, options, correct, explanation):
                sent += 1
            else:
                failed += 1
            time.sleep(POLL_SEND_DELAY)

        if failed:
            bot.send_message(chat_id, f"⚠️ اكتمل الإرسال: {sent} نجح، {failed} فشل.")
    else:
        bot.send_message(chat_id, "❌ لم يتم العثور على أي سؤال مطابق للمواصفات.")

    # ─── إبلاغ عن الأسئلة المرفوضة ───
    if invalid_blocks:
        bot.send_message(
            chat_id,
            f"⚠️ {len(invalid_blocks)} سؤال تم رفضه — اقرأ التفاصيل أدناه:",
        )
        report_parts = [
            f"سبب الرفض: {reason}\n{block_text}"
            for block_text, reason in invalid_blocks
        ]
        full_report = "\n\n---\n\n".join(report_parts)
        for i in range(0, len(full_report), 4000):
            bot.send_message(chat_id, full_report[i : i + 4000])


# ─────────────────────────────────────────────
# Handlers
# ─────────────────────────────────────────────

START_MSG = (
    "👋 أهلاً! أنا بوت تحويل الأسئلة إلى Polls.\n\n"
    "📤 أرسل أسئلتك مباشرة كنص، أو ارفع ملف PDF / Word.\n\n"
    "أرسل /help لعرض الصيغة المطلوبة."
)

HELP_MSG = (
    "📋 الصيغة المطلوبة:\n\n"
    "1. نص السؤال هنا\n"
    "A. اختيار أول\n"
    "B. اختيار صح *\n"
    "C. اختيار ثالث\n"
    "D. اختيار رابع\n"
    "? الشرح (اختياري)\n\n"
    "────────────────────\n"
    "📌 قواعد مهمة:\n"
    "• ضع * بجانب الإجابة الصحيحة\n"
    "• اختيارين على الأقل، و10 كحد أقصى\n"
    "• السؤال: 300 حرف كحد أقصى\n"
    "• كل اختيار: 100 حرف كحد أقصى\n"
    "• الشرح: 200 حرف كحد أقصى\n\n"
    "📁 الملفات المدعومة: PDF — Word (.docx)"
)


@bot.message_handler(commands=["start"])
def cmd_start(message):
    bot.send_message(message.chat.id, START_MSG)


@bot.message_handler(commands=["help"])
def cmd_help(message):
    bot.send_message(message.chat.id, HELP_MSG)


@bot.message_handler(content_types=["text"])
def handle_text(message):
    if message.text and message.text.startswith("/"):
        bot.reply_to(message, "❓ أمر غير معروف. أرسل /help للمساعدة.")
        return
    process_and_send(message.chat.id, message.text or "")


@bot.message_handler(content_types=["document"])
def handle_document(message):
    doc = message.document

    # ─── التحقق من حجم الملف ───
    if doc.file_size and doc.file_size > MAX_FILE_SIZE_BYTES:
        bot.reply_to(
            message,
            f"❌ حجم الملف ({doc.file_size // (1024 * 1024)} MB) يتجاوز الحد المسموح (20 MB).",
        )
        return

    file_name = doc.file_name or ""
    extension = os.path.splitext(file_name)[1].lower()
    mime      = doc.mime_type or ""

    is_pdf     = extension == ".pdf"  or mime == "application/pdf"
    is_docx    = extension == ".docx" or mime == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    is_old_doc = extension == ".doc"

    if is_old_doc:
        bot.reply_to(
            message,
            "⚠️ صيغة .doc القديمة غير مدعومة.\n"
            "احفظ الملف بصيغة .docx وأعد الإرسال.",
        )
        return

    if not (is_pdf or is_docx):
        bot.reply_to(
            message,
            "⚠️ صيغة الملف غير مدعومة.\n"
            "البوت يقبل فقط: PDF أو Word (.docx)",
        )
        return

    # ─── تحميل الملف ───
    status_msg = bot.reply_to(message, "⏳ جاري تحميل الملف...")
    try:
        file_info  = bot.get_file(doc.file_id)
        downloaded = bot.download_file(file_info.file_path)
    except ApiTelegramException as e:
        bot.edit_message_text(
            f"❌ فشل تحميل الملف:\n{e}",
            message.chat.id, status_msg.message_id,
        )
        return
    except Exception as e:
        bot.edit_message_text(
            f"❌ خطأ غير متوقع أثناء التحميل:\n{e}",
            message.chat.id, status_msg.message_id,
        )
        return

    # ─── حفظ مؤقت وقراءة ───
    suffix   = ".pdf" if is_pdf else ".docx"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(downloaded)
            tmp_path = tmp.name

        bot.edit_message_text(
            "⏳ جاري قراءة وتحليل الملف...",
            message.chat.id, status_msg.message_id,
        )

        extracted = extract_text_from_pdf(tmp_path) if is_pdf else extract_text_from_docx(tmp_path)

        if not extracted:
            tip = (
                "تأكد أن الملف يحتوي على نص قابل للنسخ وليس صوراً ممسوحة."
                if is_pdf else
                "تأكد أن الملف ليس تالفاً وأنه بصيغة .docx وليس .doc."
            )
            bot.edit_message_text(
                f"❌ لم أتمكن من استخراج النص من الملف.\n{tip}",
                message.chat.id, status_msg.message_id,
            )
            return

        # حذف رسالة الحالة قبل البدء بالإرسال
        try:
            bot.delete_message(message.chat.id, status_msg.message_id)
        except Exception:
            pass

        process_and_send(message.chat.id, extracted)

    except Exception as e:
        logger.exception("خطأ غير متوقع في handle_document")
        bot.send_message(message.chat.id, f"❌ حدث خطأ أثناء معالجة الملف:\n{e}")

    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


# ─────────────────────────────────────────────
# نقطة الدخول
# ─────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("جاري بدء تشغيل البوت...")

    try:
        bot.delete_webhook()
        logger.info("تم حذف الـ Webhook بنجاح.")
    except ApiTelegramException as e:
        logger.warning(f"تعذّر حذف الـ Webhook: {e}")
    except Exception as e:
        logger.warning(f"خطأ غير متوقع عند حذف الـ Webhook: {e}")

    logger.info("البوت يعمل الآن...")
    bot.infinity_polling(
        timeout=20,
        long_polling_timeout=10,
        logger_level=logging.WARNING,
    )
