import telebot
from telebot import types
import datetime
import time
from threading import Thread, Timer
import random
import config
import signal
import requests
from requests.exceptions import ReadTimeout, ConnectionError

import sqlite3

# База данных и конфигурация
def init_db():
    conn = sqlite3.connect(config.DATABASE)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                status TEXT,
                last_request_time DATETIME,
                roles TEXT)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS requests (
                request_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                status TEXT,
                request_time DATETIME,
                FOREIGN KEY(user_id) REFERENCES users(user_id))''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS numbers (
                number_id INTEGER PRIMARY KEY AUTOINCREMENT,
                number TEXT,
                service TEXT,
                user_id INTEGER,
                issued_to INTEGER,
                issued_time DATETIME,
                success INTEGER DEFAULT 0,
                add_date DATE,
                add_time TIME,
                FOREIGN KEY(user_id) REFERENCES users(user_id))''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS stats (
                date DATE PRIMARY KEY,
                whatsapp_success INTEGER,
                whatsapp_total INTEGER,
                telegram_success INTEGER,
                telegram_total INTEGER)''')

    c.execute('''CREATE TABLE IF NOT EXISTS counter (
                id INTEGER PRIMARY KEY,
                count INTEGER)''')

    c.execute("INSERT OR IGNORE INTO counter (id, count) VALUES (1, 0)")
    c.execute("INSERT OR IGNORE INTO users (user_id, username, status, roles) VALUES (?, ?, ?, ?)",
              (config.ADMIN_ID, 'main_admin', 'approved', 'admin'))

    conn.commit()
    conn.close()

def execute_query(query, params=()):
    conn = sqlite3.connect(config.DATABASE)
    c = conn.cursor()
    c.execute(query, params)
    conn.commit()
    conn.close()

def fetch_all(query, params=()):
    conn = sqlite3.connect(config.DATABASE)
    c = conn.cursor()
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    return rows

def fetch_one(query, params=()):
    conn = sqlite3.connect(config.DATABASE)
    c = conn.cursor()
    c.execute(query, params)
    row = c.fetchone()
    conn.close()
    return row

def reset_counter():
    execute_query("UPDATE counter SET count = 0 WHERE id = 1")

def increment_counter():
    execute_query("UPDATE counter SET count = count + 1 WHERE id = 1")

def decrement_counter():
    execute_query("UPDATE counter SET count = count - 1 WHERE id = 1")

def get_counter():
    row = fetch_one("SELECT count FROM counter WHERE id = 1")
    return row[0] if row else 0

def update_stats(service, success):
    date = datetime.date.today()
    stats = fetch_one("SELECT * FROM stats WHERE date = ?", (date,))
    if stats:
        if service == 'whatsapp':
            if success:
                execute_query("UPDATE stats SET whatsapp_success = whatsapp_success + 1 WHERE date = ?", (date,))
            execute_query("UPDATE stats SET whatsapp_total = whatsapp_total + 1 WHERE date = ?", (date,))
        elif service == 'telegram':
            if success:
                execute_query("UPDATE stats SET telegram_success = telegram_success + 1 WHERE date = ?", (date,))
            execute_query("UPDATE stats SET telegram_total = telegram_total + 1 WHERE date = ?", (date,))
    else:
        if service == 'whatsapp':
            execute_query("INSERT INTO stats (date, whatsapp_success, whatsapp_total, telegram_success, telegram_total) VALUES (?, ?, ?, ?, ?)",
                          (date, 1 if success else 0, 1, 0, 0))
        elif service == 'telegram':
            execute_query("INSERT INTO stats (date, whatsapp_success, whatsapp_total, telegram_success, telegram_total) VALUES (?, ?, ?, ?, ?)",
                          (date, 0, 0, 1 if success else 0, 1))

def get_stats():
    date = datetime.date.today()
    stats = fetch_one("SELECT * FROM stats WHERE date = ?", (date,))
    return stats if stats else (date, 0, 0, 0, 0)

def mark_successful(number):
    execute_query("UPDATE numbers SET success = 1 WHERE number = ?", (number,))
    number_info = fetch_one("SELECT service FROM numbers WHERE number = ?", (number,))
    if number_info:
        service = number_info[0]
        update_stats(service, success=True)

def is_admin(user_id):
    user = fetch_one("SELECT roles FROM users WHERE user_id = ?", (user_id,))
    return user and 'admin' in user[0].split(',')

def can_access_admin_panel(user_id):
    if str(user_id) == config.ADMIN_ID:
        return True
    return is_admin(user_id)

def can_access_admin_list(user_id):
    return str(user_id) == config.ADMIN_ID

def store_daily_stats():
    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    stats = fetch_one("SELECT * FROM stats WHERE date = ?", (yesterday,))
    if stats:
        execute_query("INSERT OR IGNORE INTO weekly_stats (date, whatsapp_success, whatsapp_total, telegram_success, telegram_total) VALUES (?, ?, ?, ?, ?)",
                      (stats[0], stats[1], stats[2], stats[3], stats[4]))
        execute_query("DELETE FROM weekly_stats WHERE date < ?", (today - datetime.timedelta(days=7),))

def init_weekly_stats():
    execute_query('''CREATE TABLE IF NOT EXISTS weekly_stats (
                        date DATE PRIMARY KEY,
                        whatsapp_success INTEGER,
                        whatsapp_total INTEGER,
                        telegram_success INTEGER,
                        telegram_total INTEGER)''')

bot = telebot.TeleBot(config.API_TOKEN, parse_mode='HTML')
init_db()

user_data = {}
admin_data = {}
recently_issued_numbers = {}

def retry_request(func, *args, retries=3, delay=2, **kwargs):
    for attempt in range(retries):
        try:
            return func(*args, **kwargs)
        except (ReadTimeout, ConnectionError):
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                raise

def init_user_data(user_id, username):
    user = fetch_one("SELECT * FROM users WHERE user_id = ?", (user_id,))
    if not user:
        execute_query("INSERT INTO users (user_id, username, status, last_request_time, roles) VALUES (?, ?, ?, ?, ?)",
                      (user_id, username, 'pending', None, ''))
    else:
        roles = user[4].split(',')
        if str(user_id) == config.ADMIN_ID and 'admin' not in roles:
            add_role(user_id, 'admin')

def add_role(user_id, role):
    user = fetch_one("SELECT roles FROM users WHERE user_id = ?", (user_id,))
    if user:
        roles = user[0]
        if role not in roles.split(','):
            roles = roles + f",{role}" if roles else role
            execute_query("UPDATE users SET roles = ? WHERE user_id = ?", (roles, user_id))

def remove_role(user_id, role):
    user = fetch_one("SELECT roles FROM users WHERE user_id = ?", (user_id,))
    if user:
        roles = user[0].split(',')
        if role in roles:
            roles.remove(role)
            execute_query("UPDATE users SET roles = ? WHERE user_id = ?", (','.join(roles), user_id))

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    user_id = message.from_user.id
    username = message.from_user.username

    if str(user_id) == config.ADMIN_ID:
        add_role(user_id, 'admin')
        show_admin_main_menu(message)
        return

    init_user_data(user_id, username)
    user = fetch_one("SELECT status, roles FROM users WHERE user_id = ?", (user_id,))
    if user[0] == 'approved' or 'admin' in user[1].split(',') or 'worker' in user[1].split(','):
        user_data[user_id] = {'whatsapp': [], 'telegram': [], 'start_time': None, 'sms_requests': {}}
        markup = types.ReplyKeyboardMarkup(row_width=1, resize_keyboard=True)
        btn1 = types.KeyboardButton('🔄 Начать работу')
        markup.add(btn1)
        bot.send_message(message.chat.id, "Добро пожаловать!\n\n🔄 Нажмите 'Начать работу' для начала.", reply_markup=markup)
    else:
        show_pending_menu(message)

def show_pending_menu(message):
    markup = types.ReplyKeyboardMarkup(row_width=1, resize_keyboard=True)
    btn1 = types.KeyboardButton('Отправить заявку на вступление')
    btn2 = types.KeyboardButton('Мои заявки')
    markup.add(btn1, btn2)
    bot.send_message(message.chat.id, "Вы не имеете доступа к функционалу. Пожалуйста, отправьте заявку на вступление.", reply_markup=markup)

@bot.message_handler(func=lambda message: message.text == 'Отправить заявку на вступление')
def request_access(message):
    user_id = message.from_user.id
    username = message.from_user.username
    now = datetime.datetime.now()

    user = fetch_one("SELECT last_request_time FROM users WHERE user_id = ?", (user_id,))
    
    if user and user[0]:
        try:
            last_request_time = datetime.datetime.strptime(user[0], '%Y-%m-%d %H:%M:%S.%f')
        except ValueError:
            last_request_time = datetime.datetime.strptime(user[0], '%Y-%m-%d %H:%M:%S')

        if (now - last_request_time).total_seconds() < 10:
            bot.send_message(message.chat.id, "Вы можете подать заявку только через 10 секунд после последней попытки.")
            return

    execute_query("INSERT INTO requests (user_id, username, status, request_time) VALUES (?, ?, ?, ?)",
                  (user_id, username, 'pending', now))
    execute_query("UPDATE users SET last_request_time = ? WHERE user_id = ?", (now, user_id))
    
    bot.send_message(message.chat.id, "Ваша заявка на вступление отправлена.")
    bot.send_message(config.ADMIN_ID, f"Получена заявка на вступление от @{username}", reply_markup=admin_approval_markup(user_id))

def admin_approval_markup(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn1 = types.InlineKeyboardButton("Одобрить", callback_data=f"approve_{user_id}")
    btn2 = types.InlineKeyboardButton("Отказать", callback_data=f"reject_{user_id}")
    markup.add(btn1, btn2)
    return markup

@bot.callback_query_handler(func=lambda call: call.data.startswith('approve_'))
def approve_request(call):
    user_id = int(call.data.split('_')[1])
    execute_query("UPDATE users SET status = 'approved' WHERE user_id = ?", (user_id,))
    execute_query("UPDATE requests SET status = 'approved' WHERE user_id = ? AND status = 'pending'", (user_id,))
    bot.send_message(call.message.chat.id, f"Заявка на вступление пользователя с ID {user_id} одобрена.")
    bot.send_message(user_id, "Заявка на вступление одобрена. Добро пожаловать!")
    show_main_menu_by_user_id(user_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('reject_'))
def reject_request(call):
    user_id = int(call.data.split('_')[1])
    execute_query("UPDATE requests SET status = 'rejected' WHERE user_id = ? AND status = 'pending'", (user_id,))
    bot.send_message(call.message.chat.id, f"Заявка на вступление пользователя с ID {user_id} отклонена.")
    bot.send_message(user_id, "Вам отказано в доступе. Попробуйте еще раз через 24 часа, если считаете, что это ошибка.")
    bot.answer_callback_query(call.id)

def show_main_menu_by_user_id(user_id):
    markup = types.ReplyKeyboardMarkup(row_width=1, resize_keyboard=True)
    btn1 = types.KeyboardButton('➕ Добавить номера')
    btn2 = types.KeyboardButton('📊 Профиль')
    btn3 = types.KeyboardButton('📋 Добавленные номера')
    btn4 = types.KeyboardButton('⏹️ Закончить работу')
    markup.add(btn1, btn2, btn3, btn4)
    bot.send_message(user_id, "🚀 Работа начата!\n\nВыберите действие ниже.", reply_markup=markup)

def show_main_menu(message):
    markup = types.ReplyKeyboardMarkup(row_width=1, resize_keyboard=True)
    btn1 = types.KeyboardButton('➕ Добавить номера')
    btn2 = types.KeyboardButton('📊 Профиль')
    btn3 = types.KeyboardButton('📋 Добавленные номера')
    btn4 = types.KeyboardButton('⏹️ Закончить работу')
    markup.add(btn1, btn2, btn3, btn4)
    bot.send_message(message.chat.id, "🚀 Работа начата!\n\nВыберите действие ниже.", reply_markup=markup)

def show_admin_main_menu(message):
    markup = types.ReplyKeyboardMarkup(row_width=1, resize_keyboard=True)
    btn1 = types.KeyboardButton('➕ Добавить номера')
    btn2 = types.KeyboardButton('📊 Профиль')
    btn3 = types.KeyboardButton('📋 Добавленные номера')
    btn4 = types.KeyboardButton('⏹️ Закончить работу')
    btn5 = types.KeyboardButton('🔧 Войти в админ панель')
    markup.add(btn1, btn2, btn3, btn4, btn5)
    bot.send_message(message.chat.id, "🚀 Работа начата!\n\nВыберите действие ниже.", reply_markup=markup)

@bot.message_handler(func=lambda message: message.text == '🔧 Войти в админ панель')
def admin_panel(message):
    user_id = message.from_user.id
    if can_access_admin_panel(user_id):
        show_admin_panel(message)
    else:
        bot.send_message(message.chat.id, "У вас нет доступа к админ панели.")

def show_admin_panel(message):
    markup = types.ReplyKeyboardMarkup(row_width=1, resize_keyboard=True)
    btn1 = types.KeyboardButton('📊 Статистика')
    btn2 = types.KeyboardButton('👥 Список администраторов')
    btn3 = types.KeyboardButton('👥 Список работников')
    btn4 = types.KeyboardButton('🔙 Назад')
    markup.add(btn1, btn2, btn3, btn4)
    bot.send_message(message.chat.id, "🔧 Админ панель", reply_markup=markup)

@bot.message_handler(func=lambda message: message.text == '📊 Статистика')
def show_stats(message):
    bot.send_message(message.chat.id, "Статистика за последние 7 дней будет добавлена позже.")

@bot.message_handler(func=lambda message: message.text == '👥 Список администраторов')
def list_admins(message):
    if not can_access_admin_list(message.from_user.id):
        bot.send_message(message.chat.id, "У вас нет прав на просмотр этого списка.")
        return

    admins = fetch_all("SELECT user_id, username FROM users WHERE roles LIKE '%admin%'")
    response = "Список администраторов:\n\n"
    markup = types.InlineKeyboardMarkup()
    for admin in admins:
        response += f"@{admin[1]} (ID: {admin[0]})\n"
        markup.add(types.InlineKeyboardButton(f"Удалить @{admin[1]}", callback_data=f"remove_admin_{admin[0]}"))
    markup.add(types.InlineKeyboardButton('➕ Добавить', callback_data='add_admin'))
    bot.send_message(message.chat.id, response, reply_markup=markup)

@bot.message_handler(func=lambda message: message.text == '👥 Список работников')
def list_workers(message):
    workers = fetch_all("SELECT user_id, username FROM users WHERE roles LIKE '%worker%'")
    response = "Список работников:\n\n"
    markup = types.InlineKeyboardMarkup()
    for worker in workers:
        response += f"@{worker[1]} (ID: {worker[0]})\n"
        markup.add(types.InlineKeyboardButton(f"Удалить @{worker[1]}", callback_data=f"remove_worker_{worker[0]}"))
    bot.send_message(message.chat.id, response, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == 'add_admin')
def add_admin(call):
    msg = bot.send_message(call.message.chat.id, "Введите ID пользователя для добавления в администраторы:")
    bot.register_next_step_handler(msg, process_add_admin)

def process_add_admin(message):
    try:
        user_id = int(message.text)
        add_role(user_id, 'admin')
        bot.send_message(message.chat.id, f"Пользователь с ID {user_id} добавлен в администраторы.")
    except ValueError:
        bot.send_message(message.chat.id, "Пожалуйста, введите корректный ID пользователя.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('remove_admin_'))
def remove_admin(call):
    user_id = int(call.data.split('_')[2])
    remove_role(user_id, 'admin')
    bot.send_message(call.message.chat.id, f"Пользователь с ID {user_id} удален из администраторов.")
    list_admins(call.message)

@bot.callback_query_handler(func=lambda call: call.data.startswith('remove_worker_'))
def remove_worker(call):
    user_id = int(call.data.split('_')[2])
    remove_role(user_id, 'worker')
    bot.send_message(call.message.chat.id, f"Пользователь с ID {user_id} удален из работников.")
    list_workers(call.message)

@bot.message_handler(func=lambda message: message.text == '🔄 Начать работу')
def start_work(message):
    user_id = message.from_user.id
    if user_id not in user_data:
        user_data[user_id] = {'whatsapp': [], 'telegram': [], 'start_time': None, 'sms_requests': {}}
    user_data[user_id]['start_time'] = datetime.datetime.now()
    show_main_menu(message)

@bot.message_handler(func=lambda message: message.text == '➕ Добавить номера')
def add_numbers(message):
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn1 = types.InlineKeyboardButton("WhatsApp", callback_data="add_whatsapp")
    btn2 = types.InlineKeyboardButton("Telegram", callback_data="add_telegram")
    markup.add(btn1, btn2)
    back_button = types.InlineKeyboardButton('🔙 Назад', callback_data='go_back')
    markup.add(back_button)
    
    try:
        retry_request(bot.send_message, message.chat.id, "📲 Выберите, куда добавить номер:", reply_markup=markup)
    except (ReadTimeout, ConnectionError):
        bot.send_message(message.chat.id, "Ошибка сети. Пожалуйста, попробуйте снова позже.")

@bot.callback_query_handler(func=lambda call: call.data in ['add_whatsapp', 'add_telegram'])
def choose_service(call):
    service = call.data.split('_')[1]
    msg = bot.send_message(call.message.chat.id, f"Введите список номеров для {service.capitalize()} в формате 9123456789, каждый номер с новой строки:")
    bot.register_next_step_handler(msg, process_numbers, service)
    show_back_button(call.message)

def process_numbers(message, service):
    user_id = message.from_user.id
    if user_id not in user_data:
        user_data[user_id] = {'whatsapp': [], 'telegram': [], 'start_time': None, 'sms_requests': {}}
    numbers = message.text.split('\n')
    valid_numbers = []
    invalid_numbers = 0
    for number in numbers:
        if len(number) == 10 and number.isdigit():
            valid_numbers.append({'number': number, 'timestamp': datetime.datetime.now(), 'user_id': user_id})
        else:
            invalid_numbers += 1

    user_data[user_id][service].extend(valid_numbers)

    if service == 'whatsapp' and len(user_data[user_id][service]) > 25:
        user_data[user_id][service] = user_data[user_id][service][:25]

    queue_data = fetch_all("SELECT number FROM numbers WHERE service = ? AND issued_to IS NULL", (service,))
    queue_numbers = [entry[0] for entry in queue_data]
    for number in valid_numbers:
        if number['number'] not in queue_numbers:
            execute_query("INSERT INTO numbers (number, service, user_id, add_date, add_time) VALUES (?, ?, ?, ?, ?)",
                          (number['number'], service, user_id, number['timestamp'].date(), number['timestamp'].time().strftime('%H:%M:%S')))

    response = f"✅ Обработка номеров {service.capitalize()} завершена!\n\n"
    response += f"➕ Добавлено записей: {len(valid_numbers)}\n"
    response += f"❌ Не удалось распознать: {invalid_numbers}\n"
    response += f"🔢 Обработано записей: {len(numbers)}\n"
    response += f"📋 Ваших номеров в очереди: {len(user_data[user_id][service])}\n"
    bot.send_message(message.chat.id, response)
    show_back_button(message)

@bot.message_handler(func=lambda message: message.text == '📊 Профиль')
def show_profile(message):
    user_id = message.from_user.id
    if user_id not in user_data:
        bot.send_message(message.chat.id, "Сначала начните работу, чтобы видеть статистику.")
        return

    stats = get_stats()
    whatsapp_success, whatsapp_total = stats[1], stats[2]
    telegram_success, telegram_total = stats[3], stats[4]

    whatsapp_earnings = whatsapp_success * 3.2
    telegram_earnings = telegram_success * 1.8

    response = f"🧸 Вы {message.from_user.username}\n"
    response += f"Статистика за {datetime.date.today().strftime('%d-%m-%Y')}\n"
    response += f"🟢 WhatsApp:\n"
    response += f"Удачных: {whatsapp_success}\n"
    response += f"Слетевших: {whatsapp_total - whatsapp_success}\n"
    response += f"Всего: {whatsapp_total}\n"
    response += f"За сегодня вы заработали: {whatsapp_earnings}$\n\n"
    response += f"🔵 Telegram:\n"
    response += f"Удачных: {telegram_success}\n"
    response += f"Слетевших: {telegram_total - telegram_success}\n"
    response += f"Всего: {telegram_total}\n"
    response += f"За сегодня вы заработали: {telegram_earnings}$\n"
    
    try:
        retry_request(bot.send_message, message.chat.id, response)
    except (ReadTimeout, ConnectionError):
        bot.send_message(message.chat.id, "Ошибка сети. Пожалуйста, попробуйте снова позже.")

@bot.message_handler(func=lambda message: message.text == '📋 Добавленные номера')
def show_added_numbers(message):
    user_id = message.from_user.id
    if user_id not in user_data:
        bot.send_message(message.chat.id, "Сначала начните работу, чтобы видеть добавленные номера.")
        return

    page = 0
    show_numbers_page(message, user_id, page)

def show_numbers_page(message, user_id, page):
    markup = types.InlineKeyboardMarkup()
    numbers = user_data[user_id]['whatsapp'] + user_data[user_id]['telegram']
    start_index = page * 4
    end_index = start_index + 4
    numbers_page = numbers[start_index:end_index]

    for entry in numbers_page:
        number = entry['number']
        timestamp = entry['timestamp']
        service = "🟢" if number in [num['number'] for num in user_data[user_id]['whatsapp']] else "🔵"
        btn_text = f"{service} {timestamp.strftime('%Y-%m-%d %H:%M')} - {number}"
        markup.add(types.InlineKeyboardButton(btn_text, callback_data=f"delete_{number}_{timestamp.strftime('%Y%m%d%H%M')}"))

    if start_index > 0:
        markup.add(types.InlineKeyboardButton('⬅️ Назад', callback_data=f'prev_page_{page-1}'))
    if end_index < len(numbers):
        markup.add(types.InlineKeyboardButton('➡️ Вперед', callback_data=f'next_page_{page+1}'))

    markup.add(types.InlineKeyboardButton('🔙 Назад', callback_data='go_back'))
    bot.send_message(message.chat.id, "Ваши номера:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('delete_'))
def delete_number(call):
    _, number, timestamp = call.data.split('_', 2)
    timestamp = datetime.datetime.strptime(timestamp, '%Y%m%d%H%M')
    user_id = call.from_user.id
    for service in ['whatsapp', 'telegram']:
        for entry in user_data[user_id][service]:
            if entry['number'] == number and entry['timestamp'] == timestamp:
                user_data[user_id][service].remove(entry)
                execute_query("DELETE FROM numbers WHERE number = ?", (number,))
                bot.send_message(user_id, f"Номер {number} удален из очереди!")
                bot.edit_message_text("Ваши номера:", call.message.chat.id, call.message.message_id, reply_markup=None)
                show_numbers_page(call.message, user_id, 0)
                return

@bot.callback_query_handler(func=lambda call: call.data.startswith('prev_page_') or call.data.startswith('next_page_'))
def handle_pagination(call):
    user_id = call.from_user.id
    page = int(call.data.split('_')[-1])
    bot.delete_message(call.message.chat.id, call.message.message_id)
    show_numbers_page(call.message, user_id, page)

@bot.message_handler(func=lambda message: message.text == '⏹️ Закончить работу')
def end_work(message):
    user_id = message.from_user.id
    if user_id not in user_data:
        user_data[user_id] = {'whatsapp': [], 'telegram': [], 'start_time': None, 'sms_requests': {}}

    user_data[user_id]['start_time'] = None
    user_data[user_id]['whatsapp'] = []
    user_data[user_id]['telegram'] = []
    bot.send_message(message.chat.id, "⏹️ Работа завершена. Все ваши номера удалены из очереди.")

@bot.message_handler(commands=['rm'])
def remove_numbers(message):
    numbers = message.text.split()[1:]
    for number in numbers:
        execute_query("DELETE FROM numbers WHERE number = ?", (number,))
        bot.send_message(message.chat.id, f"Номер {number} удален из базы данных.")

@bot.message_handler(func=lambda message: message.text.lower() in ['вотс', 'телега'])
def handle_purchase(message):
    service = 'whatsapp' if message.text.lower() == 'вотс' else 'telegram'
    queue_data = fetch_all("SELECT * FROM numbers WHERE service = ? AND issued_to IS NULL", (service,))
    if queue_data:
        number_entry = random.choice(queue_data)
        number = number_entry[1]
        user_id = number_entry[3]
        execute_query("UPDATE numbers SET issued_to = ?, issued_time = ? WHERE number = ?",
                      (message.from_user.id, datetime.datetime.now(), number))
        if service not in recently_issued_numbers:
            recently_issued_numbers[service] = []
        recently_issued_numbers[service].append(number)
        if len(recently_issued_numbers[service]) > len(queue_data):
            recently_issued_numbers[service].pop(0)
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton('Запросить СМС', callback_data=f'request_sms_{number}'),
            types.InlineKeyboardButton('Замена', callback_data=f'replace_number_{number}'),
            types.InlineKeyboardButton('❌Слёт', callback_data=f'decrement_counter_{number}')
        )
        bot.send_message(message.chat.id, f"<b>Номер:</b> <a href='tel:{number}'>{number}</a>", reply_markup=markup)
        bot.send_message(user_id, f"Номер {number} был выдан пользователю {message.from_user.username}.")
        increment_counter()
        update_stats(service, success=False)
        Timer(600, finalize_number_status, args=(number, message)).start()
    else:
        bot.send_message(message.chat.id, f"Нет доступных номеров для {service.capitalize()}.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('request_sms_'))
def request_sms(call):
    number = call.data.split('_')[2]
    bot.send_message(call.message.chat.id, f"Ожидание СМС. Номер: <a href='tel:{number}'>{number}</a>")
    worker_id = fetch_one("SELECT user_id FROM numbers WHERE number = ?", (number,))
    if worker_id:
        worker_id = worker_id[0]
        request_msg = bot.send_message(worker_id, f"Запрошен СМС по номеру {number}. Пришлите смс ответом на это сообщение.", reply_markup=worker_sms_markup(number))
        if worker_id not in user_data:
            user_data[worker_id] = {'whatsapp': [], 'telegram': [], 'start_time': None, 'sms_requests': {}}
        user_data[worker_id]['sms_requests'][number] = request_msg.message_id
        bot.register_next_step_handler(request_msg, receive_sms, number)
    bot.answer_callback_query(call.id)

def worker_sms_markup(number):
    markup = types.InlineKeyboardMarkup(row_width=1)
    cancel_button = types.InlineKeyboardButton('Отказаться', callback_data=f'cancel_sms_{number}')
    markup.add(cancel_button)
    return markup

def receive_sms(message, number):
    user_id = message.from_user.id
    if user_id in user_data and 'sms_requests' in user_data[user_id] and number in user_data[user_id]['sms_requests']:
        issued_to = fetch_one("SELECT issued_to FROM numbers WHERE number = ?", (number,))
        if issued_to:
            issued_to = issued_to[0]
            response = f"Номер: <a href='tel:{number}'>{number}</a>\n<b>SMS:</b> {message.text}\n+{get_counter()}"
            bot.send_message(config.GROUP_ID, response)
            del user_data[user_id]['sms_requests'][number]
        else:
            bot.send_message(message.chat.id, "Запрос на получение СМС по этому номеру не найден.")
    else:
        bot.send_message(message.chat.id, "Запрос на получение СМС по этому номеру не найден.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('replace_number_'))
def replace_number(call):
    number = call.data.split('_')[2]
    service_data = fetch_one("SELECT service FROM numbers WHERE number = ?", (number,))
    if service_data:
        service = service_data[0]
        queue_data = fetch_all("SELECT number FROM numbers WHERE service = ? AND issued_to IS NULL", (service,))
        queue_numbers = [entry[0] for entry in queue_data if entry[0] not in recently_issued_numbers.get(service, [])]
        if not queue_numbers:
            queue_numbers = [entry[0] for entry in queue_data]
            recently_issued_numbers[service] = []
        if queue_numbers:
            new_number = random.choice(queue_numbers)
            recently_issued_numbers[service].append(new_number)
            if len(recently_issued_numbers[service]) > len(queue_data):
                recently_issued_numbers[service].pop(0)
            execute_query("UPDATE numbers SET issued_to = ?, issued_time = ? WHERE number = ?",
                          (call.from_user.id, datetime.datetime.now(), new_number))
            execute_query("UPDATE numbers SET issued_to = NULL, issued_time = NULL WHERE number = ?", (number,))
            markup = types.InlineKeyboardMarkup(row_width=1)
            sms_button = types.InlineKeyboardButton('Запросить СМС', callback_data=f'request_sms_{new_number}')
            replace_button = types.InlineKeyboardButton('Замена', callback_data=f'replace_number_{new_number}')
            decrement_button = types.InlineKeyboardButton('❌Слёт', callback_data=f'decrement_counter_{new_number}')
            markup.add(sms_button, replace_button, decrement_button)
            bot.edit_message_text(f"Новый номер: <a href='tel:{new_number}'>{new_number}</a>", call.message.chat.id, call.message.message_id, reply_markup=markup)
        else:
            bot.send_message(call.message.chat.id, "Нет доступных номеров для замены.")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('cancel_sms_'))
def cancel_sms(call):
    number = call.data.split('_')[2]
    worker_id = fetch_one("SELECT user_id FROM numbers WHERE number = ?", (number,))[0]
    if worker_id and worker_id in user_data and 'sms_requests' in user_data[worker_id] and number in user_data[worker_id]['sms_requests']:
        request_msg_id = user_data[worker_id]['sms_requests'][number]
        bot.delete_message(worker_id, request_msg_id)
        del user_data[worker_id]['sms_requests'][number]
        bot.send_message(worker_id, f"Вы отказались от номера {number}")
        bot.send_message(config.GROUP_ID, f"Отказ по номеру {number}, попробуйте заново.")
        execute_query("UPDATE numbers SET issued_to = NULL, issued_time = NULL WHERE number = ?", (number,))
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('decrement_counter_'))
def decrement_counter(call):
    number = call.data.split('_')[2]
    decrement_counter()
    execute_query("DELETE FROM numbers WHERE number = ?", (number,))
    bot.send_message(call.message.chat.id, f"Номер {number} слетел. Счетчик уменьшен.")
    bot.answer_callback_query(call.id)

def finalize_number_status(number, message):
    number_info = fetch_one("SELECT success FROM numbers WHERE number = ?", (number,))
    if number_info and number_info[0] == 0:
        bot.send_message(message.chat.id, f"Номер {number} не был подтвержден в течение 10 минут и считается слетевшим.")
        execute_query("DELETE FROM numbers WHERE number = ?", (number,))
    else:
        mark_successful(number)
        bot.send_message(message.chat.id, f"Номер {number} успешно подтвержден и добавлен в удачные.")

def auto_clear():
    while True:
        now = datetime.datetime.now()
        if now.hour == 2 and now.minute == 0:
            execute_query("DELETE FROM numbers")
            reset_counter()
            for user_id in user_data:
                user_data[user_id]['whatsapp'] = []
                user_data[user_id]['telegram'] = []
            bot.send_message(config.ADMIN_ID, f"🔄 Автоматический сброс номеров завершен.")
            time.sleep(60)

Thread(target=auto_clear).start()

def show_back_button(message):
    markup = types.InlineKeyboardMarkup()
    back_button = types.InlineKeyboardButton('🔙 Назад', callback_data='go_back')
    markup.add(back_button)
    bot.send_message(message.chat.id, "🔙 Нажмите 'Назад' для возврата в главное меню.", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == 'go_back')
def handle_back(call):
    show_main_menu(call.message)
    bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda message: message.text == 'Подать заявку на доступ')
def request_access(message):
    user_id = message.from_user.id
    bot.send_message(config.ADMIN_ID, f"Новая заявка на доступ от пользователя @{message.from_user.username} (ID: {user_id}).", reply_markup=admin_approval_markup(user_id))
    bot.send_message(message.chat.id, "Ваша заявка отправлена. Ожидайте подтверждения.")

@bot.message_handler(func=lambda message: message.text == 'Мои заявки')
def view_requests(message):
    user_id = message.from_user.id
    if str(user_id) == config.ADMIN_ID:
        pending_requests = fetch_all("SELECT * FROM requests WHERE status = 'pending'")
        if pending_requests:
            response = "Заявки ожидающие подтверждения:\n\n"
            markup = types.InlineKeyboardMarkup(row_width=1)
            for req in pending_requests:
                response += f"Пользователь: @{req['username']} (ID: {req['user_id']})\n"
                markup.add(types.InlineKeyboardButton(f"Заявка от @{req['username']}", callback_data=f"show_request_{req['user_id']}"))
            bot.send_message(message.chat.id, response, reply_markup=markup)
        else:
            bot.send_message(message.chat.id, "Нет заявок ожидающих подтверждения.")
    else:
        bot.send_message(message.chat.id, "У вас нет прав на просмотр заявок.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('show_request_'))
def show_request(call):
    user_id = int(call.data.split('_')[2])
    request = fetch_one("SELECT * FROM requests WHERE user_id = ? AND status = 'pending'", (user_id,))
    if request:
        response = f"Новая заявка от @{request['username']} (ID: {request['user_id']})\n"
        response += "Одобрить или Отказать?"
        bot.send_message(call.message.chat.id, response, reply_markup=admin_approval_markup(user_id))
    else:
        bot.send_message(call.message.chat.id, "Заявка уже обработана.")
    bot.answer_callback_query(call.id)

@bot.message_handler(commands=['admin_stats'])
def admin_stats(message):
    user_id = message.from_user.id
    if str(user_id) == config.ADMIN_ID:
        response = "📊 Статистика пользователей:\n"
        for user in user_data:
            response += f"/uid{user} [@{bot.get_chat(user).username}]: WhatsApp {len(user_data[user]['whatsapp'])}/0/0/0/0 Telegram: {len(user_data[user]['telegram'])}/0\n"
        bot.send_message(message.chat.id, response)
    else:
        bot.send_message(message.chat.id, "У вас нет прав на просмотр этой информации.")

@bot.message_handler(commands=['removeworker'])
def remove_worker(message):
    if str(message.from_user.id) != config.ADMIN_ID:
        bot.send_message(message.chat.id, "У вас нет прав на выполнение этой команды.")
        return

    try:
        user_id = int(message.text.split()[1])
        execute_query("DELETE FROM users WHERE user_id = ?", (user_id,))
        execute_query("DELETE FROM requests WHERE user_id = ?", (user_id,))
        execute_query("DELETE FROM numbers WHERE user_id = ?", (user_id,))
        execute_query("DELETE FROM numbers WHERE issued_to = ?", (user_id,))
        bot.send_message(message.chat.id, f"Пользователь с ID {user_id} удален из списка работников.")
        bot.send_message(user_id, "Ваш доступ к функционалу бота был отозван. Пожалуйста, подайте заявку на вступление.")
    except (IndexError, ValueError):
        bot.send_message(message.chat.id, "Пожалуйста, укажите корректный ID пользователя после команды /removeworker.")

def signal_handler(signal, frame):
    print('Остановка бота...')
    bot.stop_polling()
    exit(0)

signal.signal(signal.SIGINT, signal_handler)

if __name__ == '__main__':
    add_role(config.ADMIN_ID, 'admin')
    try:
        bot.polling(none_stop=True, timeout=30)
    except KeyboardInterrupt:
        signal_handler(signal.SIGINT, None)
