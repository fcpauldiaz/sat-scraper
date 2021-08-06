import os
import time
import random
from flask import Flask, request, render_template, session, flash, redirect, url_for, jsonify
from celery import Celery
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.keys import Keys
from os import environ
import time 
import base64
from captcha_solver import CaptchaSolver


app = Flask(__name__)
app.config['SECRET_KEY'] = environ.get('SECRET_KEY')



# Celery configuration
app.config['broker_url'] = environ.get('REDIS_URL')
app.config['result_backend'] = environ.get('REDIS_URL')

# Initialize Celery
celery = Celery(app.name, broker=app.config['broker_url'])
celery.conf.update(app.config)




def sendKeys(elem, string):
    for letter in string:
        time.sleep(0.5)
        elem.send_keys(letter)


def scraper_initial_captcha(driver):
    driver.get("https://portal.sat.gob.gt/portal/verificador-integrado/")
    time.sleep(0.5)
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
    time.sleep(0.6)
    messages = driver.find_element_by_id("formContent:msg")
    return messages


def scraper_nit(driver, nit):
    driver.find_element_by_id("formContent:selTipoConsulta_label").click()
    driver.find_element_by_id("formContent:selTipoConsulta_2").click()
    time.sleep(0.5)
    driver.find_element_by_id("formContent:pNitEmi").send_keys(nit)
    time.sleep(0.2)
    driver.find_element_by_xpath('//span[text()="Buscar"]').click()
    time.sleep(1.5)
    driver.switch_to.frame(driver.find_element_by_tag_name("iframe"))
    result = driver.find_element_by_id("formContent:j_idt19")
    results = []
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

@celery.task(bind=True)
def long_task(self):
    """Background task that runs a long function with progress reports."""
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--window-size=1920,1080")
    driver = webdriver.Chrome(executable_path='./chromedriver', options=chrome_options)
    driver.get("https://portal.sat.gob.gt/portal/verificador-integrado/")
    time.sleep(0.5)

    messages = scraper_initial_captcha(driver)
    while (len(messages.find_elements_by_xpath(".//*")) > 0):
        messages = scraper_initial_captcha(driver)

    time.sleep(0.5)
    results = scraper_nit(driver, "84797428")
    
    return {'results': results}


@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'GET':
        return render_template('index.html', email=session.get('email', ''))
    email = request.form['email']
    session['email'] = email

    # send the email
    email_data = {
        'subject': 'Hello from Flask',
        'to': email,
        'body': 'This is a test email sent from a background Celery task.'
    }
    if request.form['submit'] == 'Send':
        # send right away
        send_async_email.delay(email_data)
        flash('Sending email to {0}'.format(email))
    else:
        # send in one minute
        send_async_email.apply_async(args=[email_data], countdown=60)
        flash('An email will be sent to {0} in one minute'.format(email))

    return redirect(url_for('index'))


@app.route('/longtask', methods=['POST'])
def longtask():
    task = long_task.apply_async()
    return jsonify({}), 201, {'status': url_for('taskstatus', task_id=task.id)}


@app.route('/status/<task_id>')
def taskstatus(task_id):
    task = long_task.AsyncResult(task_id)
    if task.state == 'PENDING':
        response = {
            'state': task.state,
            'current': 0,
            'total': 1,
            'status': 'Pending...'
        }
    elif task.state != 'FAILURE':
        response = {
            'state': task.state,
            'current': task.info.get('current', 0),
            'total': task.info.get('total', 1),
            'status': task.info.get('status', '')
        }
        if 'result' in task.info:
            response['result'] = task.info['result']
    else:
        # something went wrong in the background job
        response = {
            'state': task.state,
            'current': 1,
            'total': 1,
            'status': str(task.info),  # this is the exception raised
        }
    return jsonify(response)


if __name__ == '__main__':
    app.run(debug=True)

