from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time

# 1. ვხსნით ბრაუზერს
driver = webdriver.Chrome()

try:
    # 2. გადავდივართ სასურველ საიტზე (ჩასვი შენი ლინკი)
    driver.get("https://linex.ge/login")

    # 3. ველოდებით სანამ იუზერის ველი გამოჩნდება (მაქსიმუმ 10 წამი)
    # ყურადღება: "username" და "password" შეცვალე იმ საიტის სელექტორებით (ID, Name ან XPath), სადაც შედიხარ
    wait = WebDriverWait(driver, 10)
    
    username_field = driver.find_element(By.XPATH, "//input[@name='email']")
    password_field = driver.find_element(By.XPATH, "//input[@name='password']")
    login_button = driver.find_element(By.XPATH, "//button[@type='submit']")

    # 4. ვწერთ მონაცემებს და ვაჭერთ ღილაკს
    username_field.send_keys("asdmsakdmadd@gmail.com")
    password_field.send_keys("sadsadadadsa")
    
    # მცირე პაუზა (სურვილისამებრ)
    time.sleep(1)
    
    login_button.click()

    # 5. ვაცდით რამდენიმე წამი შედეგის სანახავად
    time.sleep(5)

finally:
    # ბრაუზერის დახურვა დასრულებისას
    driver.quit()