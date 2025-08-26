import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from dateutil import parser

import usb.core
from dateutil.relativedelta import relativedelta
from escpos.printer import Usb
from flask import Flask, render_template, request, redirect, url_for
import requests  # New import for API calls

app = Flask(__name__)
app.logger.setLevel('DEBUG')  # Set to DEBUG for detailed logs

# Constants
PORT = 5000
VID = 0x04b8
PID = 0x0e27
CONFIG_FILE = 'config.json'
TASKS_FILE = 'tasks.json'
HISTORY_FILE = 'history.json'
LISTENERS_FILE = 'listeners.json'  # New file for listener configs
PRINTER_MANUFACTURER = 'Epson'
PRINTER_MODEL = 'TM-T20III'
PRINTER_CONNECTION = 'USB'

# Global data
config = {'max_history': 500, 'hostname': 'localhost', 'theme': 'system'}
tasks = []
history = []
listeners = {}  # New: e.g., {'scf': {'enabled': False, 'request_types': '6632,6634', 'interval': 10, 'last_check': None}}


def load_data():
    global config, tasks, history, listeners
    app.logger.debug("Entering load_data")
    try:
        config_path = os.path.abspath(CONFIG_FILE)
        tasks_path = os.path.abspath(TASKS_FILE)
        history_path = os.path.abspath(HISTORY_FILE)
        listeners_path = os.path.abspath(LISTENERS_FILE)  # New

        app.logger.debug(f"Checking config file: {config_path}")
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                config = json.load(f)
                app.logger.debug(f"Loaded config: {config}")
                if config.get('theme') == 'high-contrast':
                    config['theme'] = 'system'
                    app.logger.debug("Converted high-contrast theme to system")
        else:
            app.logger.warning(f"Config file not found: {config_path}")

        app.logger.debug(f"Checking tasks file: {tasks_path}")
        if os.path.exists(tasks_path):
            with open(tasks_path, 'r') as f:
                tasks = json.load(f)
                app.logger.debug(f"Loaded tasks: {tasks}")
                for task in tasks:
                    if 'enabled' not in task:
                        task['enabled'] = True
                        app.logger.debug(f"Added 'enabled' to task: {task}")
        else:
            app.logger.warning(f"Tasks file not found: {tasks_path}")

        app.logger.debug(f"Checking history file: {history_path}")
        if os.path.exists(history_path):
            with open(history_path, 'r') as f:
                history = json.load(f)
                app.logger.debug(f"Loaded history: {history}")
                for item in history:  # Add type to existing history if missing
                    if 'type' not in item:
                        item['type'] = 'task'
        else:
            app.logger.warning(f"History file not found: {history_path}")

        # New: Load listeners
        app.logger.debug(f"Checking listeners file: {listeners_path}")
        if os.path.exists(listeners_path):
            with open(listeners_path, 'r') as f:
                listeners = json.load(f)
                app.logger.debug(f"Loaded listeners: {listeners}")
        else:
            listeners = {'scf': {'enabled': False, 'request_types': '6632,6634', 'interval': 10, 'last_check': None}}
            save_listeners()
            app.logger.warning(f"Listeners file not found, created default: {listeners_path}")
    except Exception as e:
        app.logger.error(f"Error in load_data: {e}", exc_info=True)
    app.logger.debug("Exiting load_data")


def save_config():
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f)


def save_tasks():
    with open(TASKS_FILE, 'w') as f:
        json.dump(tasks, f)


def save_history():
    with open(HISTORY_FILE, 'w') as f:
        json.dump(history, f)


def save_listeners():  # New
    with open(LISTENERS_FILE, 'w') as f:
        json.dump(listeners, f)


def is_printer_connected():
    try:
        dev = usb.core.find(idVendor=VID, idProduct=PID)
        return dev is not None
    except Exception as e:
        app.logger.error(f"USB detection error: {e}")
        return False


def calculate_next(next_time_str, recurring, days=None):
    app.logger.debug(f"Calculating next time from {next_time_str} with recurring={recurring} and days={days}")
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
        app.logger.warning("Printer not connected, skipping print")
        return
    try:
        p = Usb(VID, PID, profile='TM-T20II')
        # p.profile.media_width_mm = 80  # Set paper width to 80mm
        # QR code at the top
        p.set(align='center', density=4)
        qr_url = task.get('url', '') or f"http://{config['hostname']}:{PORT}/task_page#{task['id']}"
        p.qr(qr_url, size=5, model=2)

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
        printed_task = {**task, 'print_time': datetime.now().isoformat(), 'type': 'task'}
        history.insert(0, printed_task)
        history[:] = history[:config['max_history']]
        save_history()
    except Exception as e:
        app.logger.error(f"Print error: {e}")


def print_scf_issue(issue):  # New: Custom print for SCF issues
    if not is_printer_connected():
        app.logger.warning("Printer not connected, skipping SCF issue print")
        return
    try:
        p = Usb(VID, PID, profile='TM-T20II')
        # QR code at the top (to issue HTML URL)
        p.set(align='center', density=4)
        p.qr(issue['html_url'], size=5, model=2)

        # Category: bold, large, centered (like title)
        category = issue['request_type']['title'] if 'request_type' in issue and issue[
            'request_type'] else 'Unknown Category'
        p.set(align='center', font='a', bold=True, custom_size=True, width=3, height=3, density=4)
        p.text(category + '\n')

        # Blank line
        p.text('\n')

        # Location, reported timestamp, status (smaller text)
        p.set(align='center', font='b', bold=False, custom_size=True, width=1, height=1)
        address = issue.get('address', 'Unknown Location')
        reported_at = datetime.fromisoformat(issue['created_at'].replace('Z', '+00:00')).strftime(
            '%I:%M %p, %m/%d/%Y') if 'created_at' in issue else 'Unknown'
        status = issue.get('status', 'Unknown')
        p.text(f'Location: {address}\n')
        p.text(f'Reported: {reported_at}\n')
        p.text(f'Status: {status}\n')

        # Description (if present)
        if 'description' in issue and issue['description']:
            p.text('\nDescription:\n')
            p.text(issue['description'] + '\n')

        # Blank line
        p.text('\n')

        # Print timestamp
        print_time = datetime.now().strftime('%I:%M %p, %m/%d/%Y')
        p.text(f'Printed at {print_time}\n')

        # Issue ID
        p.text(f'Issue ID:\n')
        try:
            p.barcode(str(issue['id']), 'CODE39', width=2, height=60, pos='below', align_ct=True)
        except Exception as e:
            app.logger.error(f"Barcode print error: {e}")

        # Cut
        p.cut()
        p.close()

        # Add to history
        printed_issue = {
            'type': 'scf',
            'id': issue['id'],
            'category': category,
            'summary': issue.get('summary', ''),
            'address': address,
            'reported_at': issue.get('created_at', ''),
            'status': status,
            'description': issue.get('description', ''),
            'url': issue['html_url'],
            'print_time': datetime.now().isoformat()
        }
        history.insert(0, printed_issue)
        history[:] = history[:config['max_history']]
        save_history()
    except Exception as e:
        app.logger.error(f"SCF issue print error: {e}")


def scheduler_loop():
    # Handle missed tasks on startup
    now = datetime.now(timezone.utc)  # Assuming times are in UTC
    for task in tasks:
        if not task.get('enabled', True):
            continue
        try:
            next_time_str = task['next_time']
            next_time = datetime.fromisoformat(next_time_str).replace(tzinfo=timezone.utc)
            while next_time < now:
                next_time_str = calculate_next(next_time_str, task['recurring'], task.get('days'))
                next_time = datetime.fromisoformat(next_time_str).replace(tzinfo=timezone.utc)
            task['next_time'] = next_time_str
        except Exception as e:
            app.logger.error(f"Error handling missed task {task['id']}: {e}")
    save_tasks()

    while True:
        app.logger.debug("Scheduler loop iteration started")
        try:
            now = datetime.now()
            now_utc = datetime.now(timezone.utc)
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

            # New: Check listeners (e.g., SCF)
            if 'scf' in listeners:
                scf = listeners['scf']
                if scf['enabled'] and scf.get('request_types', '').strip():
                    print("Checking SCF listener...")
                    last_check = scf.get('last_check')
                    interval = timedelta(minutes=scf['interval'])
                    last_check_dt = None
                    if last_check:
                        try:
                            # Use dateutil.parser for robust ISO8601 parsing
                            last_check_clean = last_check.strip()
                            last_check_dt = parser.parse(last_check_clean)
                            # Ensure UTC
                            if last_check_dt.tzinfo is None:
                                last_check_dt = last_check_dt.replace(tzinfo=timezone.utc)
                        except ValueError as e:
                            app.logger.warning(
                                f"Failed to parse last_check '{last_check}': {e}, using one hour ago as fallback")
                    if last_check_dt is None or (now_utc - last_check_dt) >= interval:
                        try:
                            # Set 'after' (use last hour if no valid last_check, then interval)
                            if last_check_dt is None:
                                after = (now_utc - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
                            else:
                                after = last_check_dt.strftime('%Y-%m-%dT%H:%M:%SZ')

                            # Fetch issues
                            params = {
                                'status': 'open,acknowledged',
                                'request_types': scf['request_types'],
                                'after': after,
                                'per_page': '100'
                            }
                            issues_url = "https://seeclickfix.com/api/v2/issues"
                            app.logger.info(f"Fetching SCF issues after {after} with params: {params}")
                            resp = requests.get(issues_url, params=params, timeout=10)
                            resp.raise_for_status()
                            data = resp.json()
                            issues = data.get('issues',
                                              [])  # Note: API paginates, but assuming <100 new issues per interval

                            # Sort by created_at asc and print
                            for issue in sorted(issues, key=lambda i: i['created_at']):
                                print_scf_issue(issue)

                            # Update last_check with strict format
                            scf['last_check'] = now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')
                            save_listeners()
                            app.logger.info(
                                f"SCF listener checked at {scf['last_check']}, found {len(issues)} new issues")
                        except Exception as e:
                            app.logger.error(f"SCF listener error: {e}")
                elif scf['enabled'] and not scf.get('request_types', '').strip():
                    app.logger.warning("SCF listener enabled but request_types empty; skipping check")
        except Exception as e:
            app.logger.error(f"Scheduler loop error: {e}", exc_info=True)

        # Sleep for a minute before next check
        app.logger.debug("Scheduler loop iteration complete, sleeping for 60 seconds")
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
        app.logger.error(f"Test print error: {e}")
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


# New route for listeners page
@app.route('/listener', methods=['GET', 'POST'])  # Note: singular as per your request
def listener():
    if request.method == 'POST':
        listeners['scf'] = {
            'enabled': 'enabled' in request.form,
            'request_types': request.form.get('request_types', '6632,6634'),
            'interval': int(request.form.get('interval', 10)),
            'last_check': listeners['scf'].get('last_check')  # Preserve existing last_check
        }
        save_listeners()
        return redirect(url_for('listener'))
    return render_template('listener.html', config=config, scf=listeners.get('scf', {}))


# Initialize app
load_data()
scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
scheduler_thread.start()
app.logger.debug("Initialization complete: Data loaded and scheduler started")

if __name__ == '__main__':
    app.logger.debug("Running directly via python app.py")
    app.run(host='0.0.0.0', port=PORT)
