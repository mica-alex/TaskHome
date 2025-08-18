import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta

import usb.core
from dateutil.relativedelta import relativedelta
from escpos.printer import Usb
from flask import Flask, render_template, request, redirect, url_for
import logging

# Set up logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('app.log')
    ]
)

app = Flask(__name__)

# Constants
PORT = 5000
VID = 0x04b8
PID = 0x0e27
CONFIG_FILE = 'config.json'
TASKS_FILE = 'tasks.json'
HISTORY_FILE = 'history.json'
PRINTER_MANUFACTURER = 'Epson'
PRINTER_MODEL = 'TM-T20III'
PRINTER_CONNECTION = 'USB'

# Global data
config = {'max_history': 500, 'hostname': 'localhost', 'theme': 'system'}
tasks = []
history = []


def load_data():
    global config, tasks, history
    logging.debug("Entering load_data")
    try:
        config_path = os.path.abspath(CONFIG_FILE)
        tasks_path = os.path.abspath(TASKS_FILE)
        history_path = os.path.abspath(HISTORY_FILE)

        logging.debug(f"Checking config file: {config_path}")
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                config = json.load(f)
                logging.debug(f"Loaded config: {config}")
                if config.get('theme') == 'high-contrast':
                    config['theme'] = 'system'
                    logging.debug("Converted high-contrast theme to system")
        else:
            logging.warning(f"Config file not found: {config_path}")

        logging.debug(f"Checking tasks file: {tasks_path}")
        if os.path.exists(tasks_path):
            with open(tasks_path, 'r') as f:
                tasks = json.load(f)
                logging.debug(f"Loaded tasks: {tasks}")
                for task in tasks:
                    if 'enabled' not in task:
                        task['enabled'] = True
                        logging.debug(f"Added 'enabled' to task: {task}")
        else:
            logging.warning(f"Tasks file not found: {tasks_path}")

        logging.debug(f"Checking history file: {history_path}")
        if os.path.exists(history_path):
            with open(history_path, 'r') as f:
                history = json.load(f)
                logging.debug(f"Loaded history: {history}")
        else:
            logging.warning(f"History file not found: {history_path}")
    except Exception as e:
        logging.error(f"Error in load_data: {e}", exc_info=True)
    logging.debug("Exiting load_data")


def save_config():
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f)


def save_tasks():
    with open(TASKS_FILE, 'w') as f:
        json.dump(tasks, f)


def save_history():
    with open(HISTORY_FILE, 'w') as f:
        json.dump(history, f)


def is_printer_connected():
    try:
        dev = usb.core.find(idVendor=VID, idProduct=PID)
        return dev is not None
    except Exception as e:
        logging.error(f"USB detection error: {e}")
        return False


def calculate_next(next_time_str, recurring, days=None):
    next_time = datetime.fromisoformat(next_time_str)
    if recurring == 'daily':
        return (next_time + timedelta(days=1)).isoformat()
    elif recurring == 'weekly':
        return (next_time + timedelta(days=7)).isoformat()
    elif recurring == 'monthly':
        return (next_time + relativedelta(months=1)).isoformat()
    elif recurring == 'every_weekday':
        while True:
            next_time += timedelta(days=1)
            if next_time.weekday() < 5:
                return next_time.isoformat()
    elif recurring == 'first_day_month':
        return (next_time + relativedelta(months=1, day=1)).isoformat()
    elif recurring == 'custom':
        if not days:
            days = []
        while True:
            next_time += timedelta(days=1)
            if next_time.weekday() in days:
                return next_time.isoformat()
    return next_time_str


def print_task(task):
    if not is_printer_connected():
        logging.warning("Printer not connected, skipping print")
        return
    try:
        p = Usb(VID, PID, profile='TM-T20II')
        # p.profile.media_width_mm = 80  # Set paper width to 80mm
        # QR code at the top
        p.set(align='center', density=4)
        qr_url = task.get('url', '') or f"http://{config['hostname']}:{PORT}/task_page#{task['id']}"
        p.qr(qr_url, size=6, model=2)

        # Title: bold, large, centered
        p.set(align='center', font='a', bold=True, custom_size=True, width=3, height=3, density=4)
        p.text(task['title'] + '\n')

        # Extra info: regular, left-aligned
        if 'extra' in task and task['extra']:
            # Blank line
            p.text('\n')
            p.set(align='center', font='b', bold=False, custom_size=True, width=2, height=2)
            p.text(task['extra'] + '\n')

        # Blank line
        p.text('\n')

        # Timestamp: italic, left-aligned
        print_time = datetime.now().strftime('%I:%M %p, %m/%d/%Y')
        p.set(align='center', font='b', bold=False, custom_size=True, width=1, height=1)
        p.text(f'Printed at {print_time}\n')

        # Blank line
        p.text('\n')

        # Task Type: italic, left-aligned
        task_type = 'Non-recurring' if task['recurring'] == 'none' else f"Recurring ({task['recurring'].capitalize()})"
        p.set(align='center', font='b', bold=False, custom_size=True, width=1, height=1)
        p.text(f'Task Type: {task_type}\n')

        # Task ID: italic, left-aligned
        p.text(f'Task ID: {task["id"]}\n')

        # Disable italics and cut
        p.cut()
        p.close()

        # Add to history
        printed_task = {**task, 'print_time': datetime.now().isoformat()}
        history.insert(0, printed_task)
        history[:] = history[:config['max_history']]
        save_history()
    except Exception as e:
        logging.error(f"Print error: {e}")


def scheduler_loop():
    # Handle missed tasks on startup
    now = datetime.now()
    for task in tasks[:]:
        if not task.get('enabled', True):
            continue
        next_time = datetime.fromisoformat(task['next_time'])
        if task['recurring'] == 'none':
            if next_time <= now:
                print_task(task)
                tasks.remove(task)
        else:
            while next_time < now:
                next_time = datetime.fromisoformat(
                    calculate_next(task['next_time'], task['recurring'], task.get('days')))
            task['next_time'] = next_time.isoformat()
    save_tasks()

    while True:
        now = datetime.now()
        for task in tasks[:]:
            if not task.get('enabled', True):
                continue
            next_time = datetime.fromisoformat(task['next_time'])
            if next_time <= now:
                print_task(task)
                if task['recurring'] == 'none':
                    tasks.remove(task)
                else:
                    task['next_time'] = calculate_next(task['next_time'], task['recurring'], task.get('days'))
                save_tasks()
        time.sleep(60)


@app.route('/')
def index():
    status = 'Connected' if is_printer_connected() else 'Not connected'
    recent_history = history[:5]
    scheduled_tasks = [t for t in tasks if t.get('enabled', True)]
    return render_template('index.html', status=status, config=config, tasks=scheduled_tasks, history=recent_history)


@app.route('/task_page')
def task_page():
    scheduled_tasks = [t for t in tasks if t.get('enabled', True)]
    return render_template('tasks.html', config=config, tasks=scheduled_tasks, history=history)


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    global history
    if request.method == 'POST':
        if 'clear_history' in request.form:
            history = []
            save_history()
            return redirect(url_for('settings'))
        config['max_history'] = int(request.form['max_history'])
        config['hostname'] = request.form['hostname']
        config['theme'] = request.form['theme']
        save_config()
        history[:] = history[:config['max_history']]
        save_history()
        return redirect(url_for('settings'))
    printer_info = {
        'manufacturer': PRINTER_MANUFACTURER,
        'model': PRINTER_MODEL,
        'connection': PRINTER_CONNECTION,
        'status': 'Connected' if is_printer_connected() else 'Not connected'
    }
    return render_template('settings.html', config=config, printer_info=printer_info)


@app.route('/test_print', methods=['POST'])
def test_print():
    if not is_printer_connected():
        return 'Printer not connected. <a href="/settings">Back</a>'
    try:
        # Create a test task with example data
        test_task = {
            'id': str(uuid.uuid4()),
            'title': 'Test Task Print',
            'extra': 'This is a test print from TaskHome',
            'url': f"http://{config['hostname']}:{PORT}/task_page#test",
            'next_time': datetime.now().isoformat(),
            'recurring': 'none',
            'enabled': True
        }
        print_task(test_task)
        return 'Test print successful! <a href="/settings">Back</a>'
    except Exception as e:
        logging.error(f"Test print error: {e}")
        return f'Test print failed: {e}. <a href="/settings">Back</a>'


@app.route('/add_task', methods=['POST'])
def add_task():
    task = {
        'id': str(uuid.uuid4()),
        'title': request.form['title'],
        'next_time': request.form['next_time'] + ':00' if request.form['next_time'] else datetime.now().isoformat(),
        'recurring': request.form['recurring'],
        'enabled': 'enabled' in request.form
    }
    if 'extra' in request.form and request.form['extra']:
        task['extra'] = request.form['extra']
    if 'url' in request.form and request.form['url']:
        task['url'] = request.form['url']
    if task['recurring'] == 'custom':
        task['days'] = [int(d) for d in request.form.getlist('days')]
    tasks.append(task)
    save_tasks()
    return redirect(url_for('task_page'))


@app.route('/edit_task/<task_id>', methods=['GET', 'POST'])
def edit_task(task_id):
    task = next((t for t in tasks if t['id'] == task_id), None)
    if not task:
        return 'Task not found', 404
    if request.method == 'POST':
        task['title'] = request.form['title']
        task['next_time'] = request.form['next_time'] + ':00' if request.form['next_time'] else task['next_time']
        task['recurring'] = request.form['recurring']
        task['enabled'] = 'enabled' in request.form
        if 'extra' in request.form and request.form['extra']:
            task['extra'] = request.form['extra']
        else:
            task.pop('extra', None)
        if 'url' in request.form and request.form['url']:
            task['url'] = request.form['url']
        else:
            task.pop('url', None)
        if task['recurring'] == 'custom':
            task['days'] = [int(d) for d in request.form.getlist('days')]
        else:
            task.pop('days', None)
        save_tasks()
        return redirect(url_for('task_page'))
    return render_template('tasks.html', config=config, tasks=[t for t in tasks if t.get('enabled', True)],
                           history=history, edit_task=task)


@app.route('/delete_task', methods=['POST'])
def delete_task():
    task_id = request.form['id']
    global tasks
    tasks = [t for t in tasks if t['id'] != task_id]
    save_tasks()
    return redirect(url_for('task_page'))


# Initialize app
load_data()
scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
scheduler_thread.start()
logging.debug("Initialization complete: Data loaded and scheduler started")

if __name__ == '__main__':
    logging.debug("Running directly via python app.py")
    app.run(host='0.0.0.0', port=PORT)
