#!/usr/bin/env python
# coding: utf-8

# In[8]:


import requests

query = 'oak'

url = 'https://en.wikipedia.org/w/api.php'
params = {
            'action':'query',
            'format':'json',
            'list':'search',
            'utf8':1,
            'srsearch':query
        }

data = requests.get(url, params=params).json()

for i in data['query']['search']:
    print(i['title'], ' - Word count: ', i['wordcount'])


# In[9]:


subject = 'maple'
url = 'https://en.wikipedia.org/w/api.php'
params = {
        'action': 'query',
        'format': 'json',
        'titles': subject,
        'prop': 'extracts',
        'exintro': True,
        'explaintext': True,
    }

response = requests.get(url, params=params)
data = response.json()

page = next(iter(data['query']['pages'].values()))
print(page)
# print(page['extract'])


# In[ ]:





# In[7]:


import requests
from bs4 import BeautifulSoup

subject = 'Maple'

url = 'https://en.wikipedia.org/w/api.php'
params = {
            'action': 'parse',
            'page': subject,
            'format': 'json',
            'prop':'text',
            'redirects':''
        }

response = requests.get(url, params=params)
data = response.json()

raw_html = data['parse']['text']['*']
soup = BeautifulSoup(raw_html,'html.parser')
soup.find_all('p')
text = ''

for p in soup.find_all('p'):
    text += p.text

print(text)
print('Text length: ', len(text))

