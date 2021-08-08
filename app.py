import os
import time
import random
from flask import Flask, render_template, request, url_for, jsonify
from celery import Celery
from celery.signals import task_success, after_task_publish
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.keys import Keys
from dotenv import load_dotenv
from os import environ
import time 
import base64
from captcha_solver import CaptchaSolver
import redis


app = Flask(__name__)
app.config['SECRET_KEY'] = environ.get('SECRET_KEY')
load_dotenv()

# Celery configuration
app.config['broker_url'] = environ.get('REDISCLOUD_URL')
app.config['result_backend'] = environ.get('REDISCLOUD_URL')
app.config['redis_max_connections'] = int(environ.get('REDIS_MAX_CONNECTIONS'))
app.config['broker_pool_limit'] = 0

def get_redis():
    r = redis.Redis.from_url(environ.get('REDISCLOUD_URL'))
    return r

def close_connections(redis_db):
    all_clients = redis_db.client_list()
    counter = 0

    for client in all_clients:
        if int(client['idle']) >= 15:
            try:    
                redis_db.client_kill(client['addr'])
            except:
                pass
            counter += 1

# Initialize Celery
celery = Celery(app.name, broker=app.config['broker_url'],
    redis_max_connections=app.config['redis_max_connections'],
    BROKER_TRANSPORT_OPTIONS = {
        'max_connections': app.config['redis_max_connections'],
    }, broker_pool_limit=0)
celery.conf.update(app.config)



def sendKeys(elem, string):
    for letter in string:
        time.sleep(0.4)
        elem.send_keys(letter)


def scraper_initial_captcha(driver):
    driver.get("https://portal.sat.gob.gt/portal/verificador-integrado/")
    driver.switch_to.frame(driver.find_element_by_tag_name("iframe"))
    element_image = driver.find_element_by_id("formContent:j_idt28")
    # get the captcha as a base64 string
    img_base64 = driver.execute_script("""
        var ele = arguments[0];
        var cnv = document.createElement('canvas');
        cnv.width = ele.width; cnv.height = ele.height;
        cnv.getContext('2d').drawImage(ele, 0, 0);
        return cnv.toDataURL('image/jpeg').substring(22);    
        """, element_image)
    with open(r"captcha.jpg", 'wb') as f:
        f.write(base64.b64decode(img_base64))

    solver = CaptchaSolver('2captcha', api_key=environ.get('CAPTCHA_KEY'))
    raw_data = open('captcha.jpg', 'rb').read()
    captcha_solution = solver.solve_captcha(raw_data)
    print (captcha_solution)
    input_element = driver.find_element_by_id("formContent:j_idt30")
    sendKeys(input_element, captcha_solution)
    input_element.send_keys(Keys.ENTER)
    time.sleep(0.5)
    messages = driver.find_element_by_id("formContent:msg")
    return messages


def scraper_nit(driver, nit):
    results = []
    label = None
    try:
        label = driver.find_element_by_id("formContent:selTipoConsulta_label")
    except:
        new_query = None
        try:
            time.sleep(0.5)
            driver.switch_to.parent_frame()
            new_query = driver.find_element_by_id("formContent:btnNuevaConsulta")
            new_query.click()
            time.sleep(1)
            label = driver.find_element_by_id("formContent:selTipoConsulta_label")
        except Exception as e:
            print (str(e))
            pass
    if (label == None):
        return results
    label.click()
    driver.find_element_by_id("formContent:selTipoConsulta_2").click()
    time.sleep(0.5)
    driver.find_element_by_id("formContent:pNitEmi").send_keys(nit)
    time.sleep(0.2)
    driver.find_element_by_xpath('//span[text()="Buscar"]').click()
    time.sleep(1)
    driver.switch_to.frame(driver.find_element_by_tag_name("iframe"))
    result = driver.find_element_by_id("formContent:j_idt19")
    if "NO" in result.text:
        pass
    else:
        table = driver.find_element_by_id("formContent:pnlGridIncum")
        for row in table.find_elements_by_xpath(".//tr"):
            # get the text from all the td's from each row
            try:
                description = row.find_element_by_css_selector("a")
                text = description.text
                results.append(str(text))
            except Exception as e:
                print (str(e))
                pass
    return results

@celery.task(bind=True, autoretry_for=(Exception,), retry_backoff=2)
def scraper_task(self, nit_list):
    """Background tasks"""
    chrome_options = Options()
    if 'DYNO' in os.environ:
        chrome_options.binary_location = environ.get("GOOGLE_CHROME_BIN")
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--no-sandbox")
    if 'DYNO' in os.environ:
        driver = webdriver.Chrome(executable_path=environ.get("CHROMEDRIVER_PATH"), chrome_options=chrome_options)
    else:
        driver = webdriver.Chrome()
    messages = scraper_initial_captcha(driver)
    while (len(messages.find_elements_by_xpath(".//*")) > 0):
        messages = scraper_initial_captcha(driver)

    count = 0
    results = []
    for nit in nit_list:
        print (nit)
        self.update_state(state='PROGRESS', meta={'progress': int(count/len(nit_list)), "nit": nit})
        result = scraper_nit(driver,  nit)
        results.append({ 'result': result, 'nit': nit })
        count += 1
    driver.quit()
    return {'progress': 100, 'result': results}

@task_success.connect
def task_success_handler(sender, result,  **kwargs):
    print (sender, result)

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')


@app.route('/scraper', methods=['POST'])
def api_scraper():
    data = request.get_json(force=True)
    nit_list = data.get('nit')
    if nit_list is None:
        return { 'error': 'missing nit'}, 400
    task = scraper_task.apply_async([nit_list])
    return {'status_url': url_for('taskstatus', task_id=task.id)}


@app.route('/status/<task_id>')
def taskstatus(task_id):
    task = scraper_task.AsyncResult(task_id)
    if task.state == 'PENDING':
        response = {
            'state': task.state,
            'progress': 0,
            'status': 'Pending...'
        }
    elif task.state != 'FAILURE':
        response = {
            'state': task.state,
            'progress': task.info.get('progress', 0),
            'status': task.info.get('status', '')
        }
        if 'result' in task.info:
            response['result'] = task.info['result']
    else:
        # something went wrong in the background job
        response = {
            'state': task.state,
            'status': str(task.info),  # this is the exception raised
        }
    return jsonify(response)


if __name__ == '__main__':
    app.run(debug=True)


