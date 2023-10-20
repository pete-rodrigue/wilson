import requests
from bs4 import BeautifulSoup
import math
import re
import pyttsx3
engine = pyttsx3.init()



soup = BeautifulSoup(requests.get("https://factanimal.com/elephant/").content, "html.parser")

rv = []
counter = 0
list_index = 0
for el in soup.find_all('p'):
    next_string = el.text
    next_string = next_string.replace('Fact Animal', '')
    next_string = next_string.replace('Facts About Animals', '')
    if counter == 0:
        rv.append(next_string)
        counter += 1
    elif counter == 6:
        rv[list_index] += ' '
        rv[list_index] += next_string
        list_index += 1
        counter = 0
    else:
        rv[list_index] += next_string
        counter += 1



print(rv)
print(rv[0])

engine.say(rv[0])
engine.runAndWait()

