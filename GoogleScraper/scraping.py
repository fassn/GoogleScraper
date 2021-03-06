# -*- coding: utf-8 -*-

import datetime
import random
import time
import os
import abc
import csv
import codecs
from glob import glob

from GoogleScraper.proxies import Proxy
from GoogleScraper.database import db_Proxy
from GoogleScraper.parsing import get_parser_by_search_engine, parse_serp

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait  # available since 2.4.0
from selenium.webdriver.support import expected_conditions as EC  # available since 2.26.0
from selenium.webdriver.common.keys import Keys

import logging

logger = logging.getLogger(__name__)

SEARCH_MODES = ('http', 'selenium', 'http-async')


class GoogleSearchError(Exception):
    pass


class InvalidNumberResultsException(GoogleSearchError):
    pass


class MaliciousRequestDetected(GoogleSearchError):
    pass


class SeleniumMisconfigurationError(Exception):
    pass


class SeleniumSearchError(Exception):
    pass


class StopScrapingException(Exception):
    pass


"""
GoogleScraper should be as robust as possible.

There are several conditions that may stop the scraping process. In such a case,
a StopScrapingException is raised with the reason.

Important events:

- All proxies are detected and we cannot request further keywords => Stop.
- No internet connection => Stop.

- If the proxy is detected by the search engine we try to get another proxy from the pool and we
  call switch_proxy() => continue.

- If the proxy is detected by the search engine and there is no other proxy in the pool, we wait
  {search_engine}_proxy_detected_timeout seconds => continue.
    + If the proxy is detected again after the waiting time, we discard the proxy for the whole scrape.
"""


def get_base_search_url_by_search_engine(config, search_engine_name, search_mode):
    """Retrieves the search engine base url for a specific search_engine.

    This function cascades. So base urls will
    be overwritten by search_engine urls in the specific mode sections.
    On the other side, if a search engine has no special url in it' corresponding
    mode, the default one from the SCRAPING config section will be loaded.

    Args:
        search_engine_name The name of the search engine
        search_mode: The search mode that is used. selenium or http or http-async

    Returns:
        The base search url.
    """
    assert search_mode in SEARCH_MODES, 'search mode "{}" is not available'.format(search_mode)

    specific_base_url = config.get('{}_{}_search_url'.format(search_mode, search_engine_name), None)

    if not specific_base_url:
        specific_base_url = config.get('{}_search_url'.format(search_engine_name), None)

    ipfile = config.get('{}_ip_file'.format(search_engine_name), '')

    if os.path.exists(ipfile):
        with open(ipfile, 'rt') as file:
            ips = file.read().split('\n')
            random_ip = random.choice(ips)
            return random_ip

    return specific_base_url


class SearchEngineScrape(metaclass=abc.ABCMeta):
    """Abstract base class that represents a search engine scrape.
    
    Each subclass that derives from SearchEngineScrape needs to 
    implement some common functionality like setting a proxy, 
    returning the found results, caching results and pushing scraped
    data to a storage like a database or an output file.
    
    The derivation is divided in two hierarchies: First we divide child
    classes in different Transport mechanisms. Scraping can happen over 
    different communication channels like Raw HTTP, scraping with the
    selenium framework or using the an asynchronous HTTP client.
    
    The next layer is the concrete implementation of the search functionality
    of the specific search engines. This is not done in a extra derivation
    hierarchy (otherwise there would be a lot of base classes for each
    search engine and thus quite some boilerplate overhead), 
    instead we determine our search engine over the internal state
    (An attribute name self.search_engine) and handle the different search
    engines in the search function.
    
    Each mode must behave similarly: It can only scrape one search engine at the same time,
    but it may search for multiple search keywords. The initial start number may be
    set by the configuration. The number of pages that should be scraped for each
    keyword is also configurable.
    
    It may be possible to apply all the above rules dynamically for each
    search query. This means that the search page offset, the number of
    consecutive search pages may be provided for all keywords uniquely instead
    that they are the same for all keywords. But this requires also a
    sophisticated input format and more tricky engineering.
    """

    malicious_request_needles = {
        'google': {
            'inurl': '/sorry/',
            'inhtml': 'detected unusual traffic'
        },
        'bing': {},
        'yahoo': {},
        'baidu': {},
        'yandex': {},
        'ask': {},
        'blekko': {},
        'duckduckgo': {},
        'qwant': {}
    }

    def __init__(self, config, cache_manager=None, jobs=None, scraper_search=None, session=None, db_lock=None, cache_lock=None,
                 start_page_pos=1, search_engine=None, search_type=None, proxy=None, proxies=None, proxy_quarantine=None,
                 progress_queue=None):
        """Instantiate an SearchEngineScrape object.

        Args:
            TODO
        """
        # Set the config dictionary
        self.config = config

        # Set the cache manager
        self.cache_manager = cache_manager

        jobs = jobs or {}
        self.search_engine_name = search_engine
        assert self.search_engine_name, 'You need to specify an search_engine'

        self.search_engine_name = self.search_engine_name.lower()

        if not search_type:
            self.search_type = self.config.get('search_type', 'normal')
        else:
            self.search_type = search_type

        self.jobs = jobs

        # the keywords that couldn't be scraped by this worker
        self.missed_keywords = set()

        # the number of keywords
        self.num_keywords = len(self.jobs)

        # The actual keyword that is to be scraped next
        self.query = ''

        # The default pages per keywords
        self.pages_per_keyword = [1, ]

        # The number that shows how many searches have been done by the worker
        self.search_number = 1

        # The parser that should be used to parse the search engine results
        self.parser = get_parser_by_search_engine(self.search_engine_name)(config=self.config)

        # The number of results per page
        self.num_results_per_page = int(self.config.get('num_results_per_page', 10))

        # The page where to start scraping. By default the starting page is 1.
        if start_page_pos:
            self.start_page_pos = 1 if start_page_pos < 1 else start_page_pos
        else:
            self.start_page_pos = int(self.config.get('search_offset', 1))

        # The page where we are right now
        self.page_number = self.start_page_pos

        # Install the proxy if one was provided
        self.proxy = proxy
        if isinstance(proxy, Proxy):
            self.set_proxy()
            self.requested_by = self.proxy.host + ':' + self.proxy.port
        else:
            self.requested_by = 'localhost'

        self.proxies = proxies
        self.proxy_quarantine = proxy_quarantine

        # the scraper_search object
        self.scraper_search = scraper_search

        # the scrape mode
        # to be set by subclasses
        self.scrape_method = ''

        # Whether the instance is ready to run
        self.startable = True

        # set the database lock
        self.db_lock = db_lock

        # init the cache lock
        self.cache_lock = cache_lock

        # a queue to put an element in whenever a new keyword is scraped.
        # to visualize the progress
        self.progress_queue = progress_queue

        # set the session
        self.session = session

        # the current request time
        self.requested_at = None

        # The name of the scraper
        self.name = '[{}]'.format(self.search_engine_name) + self.__class__.__name__

        # How long to sleep (in seconds) after every n-th request
        self.sleeping_ranges = dict()
        self.sleeping_ranges = self.config.get(
            '{search_engine}_sleeping_ranges'.format(search_engine=self.search_engine_name),
            self.config.get('sleeping_ranges'))

        # the default timeout
        self.timeout = 5

        # the status of the thread after finishing or failing
        self.status = 'successful'

        self.html = ''

        self.keyword_planner = self.config.get('keyword_planner')

    @abc.abstractmethod
    def search(self, *args, **kwargs):
        """Send the search request(s) over the transport."""

    @abc.abstractmethod
    def set_proxy(self):
        """Install a proxy on the communication channel."""

    @abc.abstractmethod
    def switch_proxy(self, proxy):
        """Switch the proxy on the communication channel."""

    @abc.abstractmethod
    def proxy_check(self, proxy):
        """Check whether the assigned proxy works correctly and react"""

    @abc.abstractmethod
    def handle_request_denied(self, status_code):
        """Generic behaviour when search engines detect our scraping.

        Args:
            status_code: The status code of the http response.
        """
        self.status = 'Malicious request detected: {}'.format(status_code)

    def store(self):
        """Store the parsed data in the sqlalchemy scoped session."""
        assert self.session, 'No database session.'

        if self.html:
            self.parser.parse(self.html)
        else:
            self.parser = None

        with self.db_lock:

            serp = parse_serp(self.config, parser=self.parser, scraper=self, query=self.query)

            self.scraper_search.serps.append(serp)
            self.session.add(serp)
            self.session.commit()

            # store_serp_result(serp, self.config)

            if serp.num_results:
                return True
            else:
                return False

    def next_page(self):
        """Increment the page. The next search request will request the next page."""
        self.start_page_pos += 1

    def keyword_info(self):
        """Print a short summary where we are in the scrape and what's the next keyword."""
        logger.info(
            '[{thread_name}][{ip}]]Keyword: "{keyword}" with {num_pages} pages, slept {delay} seconds before '
            'scraping. {done}/{all} already scraped.'.format(
                thread_name=self.name,
                ip=self.requested_by,
                keyword=self.query,
                num_pages=self.pages_per_keyword,
                delay=self.current_delay,
                done=self.search_number,
                all=self.num_keywords
            ))

    def instance_creation_info(self, scraper_name):
        """Debug message whenever a scraping worker is created"""
        logger.info('[+] {}[{}][search-type:{}][{}] using search engine "{}". Num keywords={}, num pages for keyword={}'.format(
            scraper_name, self.requested_by, self.search_type, self.base_search_url, self.search_engine_name,
            len(self.jobs),
            self.pages_per_keyword))

    def cache_results(self):
        """Caches the html for the current request."""
        self.cache_manager.cache_results(self.parser, self.query, self.search_engine_name, self.scrape_method, self.page_number,
                      db_lock=self.db_lock)

    def _largest_sleep_range(self, search_number):
        """Sleep a given amount of time dependent on the number of searches done.

        Args:
            search_number: How many searches the worker has done yet.

        Returns:
            A range tuple which defines in which range the worker should sleep.
        """

        assert search_number >= 0
        if search_number != 0:
            s = sorted(self.sleeping_ranges.keys(), reverse=True)
            for n in s:
                if search_number % n == 0:
                    return self.sleeping_ranges[n]
        # sleep one second
        return 1, 2

    def detection_prevention_sleep(self):
        # match the largest sleep range
        self.current_delay = random.randrange(*self._largest_sleep_range(self.search_number))
        time.sleep(self.current_delay)

    def after_search(self):
        """Store the results and parse em.

        Notify the progress queue if necessary.
        """
        self.search_number += 1

        if not self.store():
            logger.debug('No results to store for keyword: "{}" in search engine: {}'.format(self.query,
                                                                                    self.search_engine_name))

        if self.progress_queue:
            self.progress_queue.put(1)
        self.cache_results()

    def before_search(self):
        """Things that need to happen before entering the search loop."""
        # check proxies first before anything
        if self.config.get('check_proxies', True) and self.proxy:
            if not self.proxy_check(self.proxy):
                self.startable = False

    def update_proxy_status(self, status, ipinfo=None, online=True):
        """Sets the proxy status with the results of ipinfo.io

        Args:
            status: A string the describes the status of the proxy.
            ipinfo: The json results from ipinfo.io
            online: Whether the proxy is usable or not.
        """
        ipinfo = ipinfo or {}

        with self.db_lock:

            proxy = self.session.query(db_Proxy).filter(self.proxy.host == db_Proxy.ip).first()
            if proxy:
                for key in ipinfo.keys():
                    setattr(proxy, key, ipinfo[key])

                proxy.checked_at = datetime.datetime.utcnow()
                proxy.status = status
                proxy.online = online

                self.session.add(proxy)
                self.session.commit()


from GoogleScraper.http_mode import HttpScrape
from GoogleScraper.selenium_mode import get_selenium_scraper_by_search_engine_name


class ScrapeWorkerFactory():
    def __init__(self, config, cache_manager=None, mode=None, proxy=None, proxies=None, proxy_quarantine=None,
                 search_engine=None, session=None, db_lock=None, cache_lock=None, scraper_search=None,
                 captcha_lock=None, progress_queue=None, browser_num=1):

        self.config = config
        self.cache_manager = cache_manager
        self.mode = mode
        self.proxy = proxy
        self.proxies = proxies
        self.proxy_quarantine = proxy_quarantine
        self.search_engine = search_engine
        self.session = session
        self.db_lock = db_lock
        self.cache_lock = cache_lock
        self.scraper_search = scraper_search
        self.captcha_lock = captcha_lock
        self.progress_queue = progress_queue
        self.browser_num = browser_num

        self.jobs = dict()

    def is_suitabe(self, job):

        return job['scrape_method'] == self.mode and job['search_engine'] == self.search_engine

    def add_job(self, job):

        query = job['query']
        page_number = job['page_number']

        if query not in self.jobs:
            self.jobs[query] = []

        self.jobs[query].append(page_number)

    def get_worker(self):

        if self.jobs:

            if self.mode == 'selenium':

                return get_selenium_scraper_by_search_engine_name(
                    self.config,
                    self.search_engine,
                    cache_manager=self.cache_manager,
                    search_engine=self.search_engine,
                    jobs=self.jobs,
                    session=self.session,
                    scraper_search=self.scraper_search,
                    cache_lock=self.cache_lock,
                    db_lock=self.db_lock,
                    proxy=self.proxy,
                    proxies=self.proxies,
                    proxy_quarantine=self.proxy_quarantine,
                    progress_queue=self.progress_queue,
                    captcha_lock=self.captcha_lock,
                    browser_num=self.browser_num,
                )

            elif self.mode == 'http':

                return HttpScrape(
                    self.config,
                    cache_manager=self.cache_manager,
                    search_engine=self.search_engine,
                    jobs=self.jobs,
                    session=self.session,
                    scraper_search=self.scraper_search,
                    cache_lock=self.cache_lock,
                    db_lock=self.db_lock,
                    proxy=self.proxy,
                    progress_queue=self.progress_queue,
                )

        return None

class KeywordPlannerScraper():

    """Scrape keywords volume search and average bids on Keyword Planner.

        Args:
            Keywords input into Keyword planner. Same keywords as the scrapejob.

        """
    def __init__(self):
        self.selector = {
            'signin_link': '//*[@id="header-links"]/a[1]',
            'email': 'Email',
            'next_button': 'next',
            'pw': 'Passwd',
            'signin_button': 'signIn',
            'search_volume': 'spkc-d',
            'textarea': 'gwt-debug-upload-text-box',
            'get_search_volume': 'gwt-debug-upload-ideas-button-content',
            'get_search_volume_failed': 'spcf-b',
            'download': 'gwt-debug-search-download-button',
            'download_link': 'gwt-debug-download-button-content',
            'downloadfailed': 'spee-b',
            'savefile': 'gwt-debug-retrieve-download-content',
        }

    def keyword_planner_scraper(self, keywords, browser):
        # do the actual work
        driver = self.login_keyword_planner(browser)

        if isinstance(keywords, list):
            results = {}
            while len(keywords) > 0:
                self.drive_into_keyword_planner(driver, keywords)
                results.update(self.parse_results_keyword_planner())
                keywords.pop(0)
                if len(keywords) > 0:
                    driver.get('https://adwords.google.com/KeywordPlanner')
        else:
            self.drive_into_keyword_planner(driver, keywords)
            results = self.parse_results_keyword_planner()


        driver.quit()
        return results

    def login_keyword_planner(self, browser):

        """log into Google's Keyword Planner tool. Your username (typically a gmail account)
        and password needs to be stored into environment variables. Respectively under MAIL_USERNAME and
        MAIL_PASSWORD.
        """

        # creates the webdriver instance with a profile to prevent the Save Dialog from appearing
        if browser == 'chrome':
            try:

                chromeOptions = webdriver.ChromeOptions()
                prefs = {"download.default_directory" : os.getcwd()}
                chromeOptions.add_experimental_option('prefs',prefs)

                driver = webdriver.Chrome(chrome_options=chromeOptions)

            except WebDriverException as e:
                # we don't have a chrome executable or a chrome webdriver installed
                raise

        elif browser == 'phantomjs':
            try:

                dcap = dict(DesiredCapabilities.PHANTOMJS)
                dcap["phantomjs.page.settings.userAgent"] = random_user_agent(only_desktop=True)

                driver = webdriver.PhantomJS(desired_capabilities=dcap)

            except WebDriverException as e:
                logger.error(e)

        elif browser == 'firefox':
            try:

                profile = webdriver.FirefoxProfile()
                profile.set_preference("browser.download.folderList",2)
                profile.set_preference("browser.download.manager.showWhenStarting", False)
                profile.set_preference("browser.download.dir", os.getcwd())
                profile.set_preference("browser.helperApps.neverAsk.saveToDisk",'text/csv')

                driver = webdriver.Firefox(firefox_profile=profile)

            except WebDriverException as e:
                logger.error(e)

        driver.get('https://adwords.google.com/KeywordPlanner')

        # click on the 'Sign In' link on top right of the page
        WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.XPATH, self.selector['signin_link'])))
        signin_link = driver.find_element_by_xpath(self.selector['signin_link'])
        signin_link.click()

        # fill in the Email form
        WebDriverWait(driver, 10).until(EC.visibility_of_element_located((By.ID, self.selector['email'])))
        email = driver.find_element_by_id(self.selector['email'])
        email.send_keys(os.environ.get('MAIL_USERNAME') + Keys.ENTER)

        # # click on the Next button
        # WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.ID, self.selector['next_button'])))
        # next_button = driver.find_element_by_id(self.selector['next_button'])
        # next_button.click()

        # fill in the Password form
        WebDriverWait(driver, 3).until(EC.visibility_of_element_located((By.ID, self.selector['pw'])))
        pw = driver.find_element_by_id(self.selector['pw'])
        pw.send_keys(os.environ.get('MAIL_PASSWORD') + Keys.ENTER)

        # # click on the 'Sign In' button
        # WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.ID, self.selector['signin_button'])))
        # signin = driver.find_element_by_id(self.selector['signin_button'])
        # signin.click()

        return driver

    def drive_into_keyword_planner(self, driver, keywords):
        # click on the tool 'Get search volume data and trends'
        WebDriverWait(driver, 30).until(EC.visibility_of_element_located((By.CLASS_NAME, self.selector['search_volume'])))
        search_volume = driver.find_elements_by_class_name(self.selector['search_volume'])
        search_volume[1].click()

        # fill in the Keywords form
        WebDriverWait(driver, 3).until(EC.visibility_of_element_located((By.ID, self.selector['textarea'])))
        textarea = driver.find_element_by_id(self.selector['textarea'])
        textarea.send_keys(keywords)

        # click on 'Get search volume'
        WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.ID, self.selector['get_search_volume'])))
        get_search_volume = driver.find_element_by_id(self.selector['get_search_volume'])
        get_search_volume.click()

        # this loop makes sure Keyword Planner does get the volume search for the keywords.
        # Sometimes it fails so it will loop until we get the results
        try:
            while True:
                time.sleep(5)
                # WebDriverWait(driver, 30).until(EC.visibility_of_element_located((By.CLASS_NAME, self.selector['get_search_volume_failed'])))
                get_search_volume_failed = driver.find_elements_by_class_name(self.selector['get_search_volume_failed'])
                if get_search_volume_failed[0].text == 'There was a problem retrieving ideas, please try again.':
                    time.sleep(5)
                    get_search_volume = driver.find_element_by_id(self.selector['get_search_volume'])
                    get_search_volume.click()
                else:
                    break
        except NoSuchElementException:
            pass

        # click on the Download button
        WebDriverWait(driver, 30).until(EC.element_to_be_clickable((By.ID, self.selector['download'])))
        download = driver.find_element_by_id(self.selector['download'])
        download.click()

        # a popup should appear about our download, this clicks on the new Download button
        WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.ID, self.selector['download_link'])))
        download_link = driver.find_element_by_id(self.selector['download_link'])
        download_link.click()

        # this loop makes sure Keyword Planner does prepare our output file.
        # Sometimes it fails so it will loop until it works
        try:
            while True:
                time.sleep(5)
                # WebDriverWait(driver, 30).until(EC.visibility_of_element_located((By.CLASS_NAME, self.selector['downloadfailed'])))
                downloadfailed = driver.find_elements_by_class_name(self.selector['downloadfailed'])
                if downloadfailed[0].text == 'Your download operation failed. Please try again.':
                    time.sleep(5)
                    download_link = driver.find_element_by_id(self.selector['download_link'])
                    download_link.click()
                else:
                    break
        except NoSuchElementException:
            pass

        # click on the final 'Save file' button
        WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.ID, self.selector['savefile'])))
        savefile = driver.find_element_by_id(self.selector['savefile'])
        savefile.click()

    def parse_results_keyword_planner(self):

        """parse the newly Keyword Planner output file into a dict"""

        # these 2 lines help to find the file we just downloaded,
        # since it is not possible to change the filename at save
        time.sleep(5)
        d = datetime.datetime.now()
        dday = '0' + str(d.day) if len(str(d.day)) == 1 else str(d.day)
        dmonth = '0' + str(d.month) if len(str(d.month)) == 1 else str(d.month)
        filenames = glob('Keyword Planner ' + str(d.year) + '-' + str(dmonth) + '-' + str(dday) + ' at ' + '*.csv')
        keyword_planner_results_as_a_dict = {}

        for filename in filenames:
            with codecs.open(filename, 'r', encoding='utf-16') as f:
                results=csv.DictReader(f, dialect='excel-tab')
                for r in results:
                    keyword_planner_results_as_a_dict['Keyword'] = r['Keyword']
                    keyword_planner_results_as_a_dict[r['Keyword']] = {
                        'avg_monthly_search': r['Avg. Monthly Searches (exact match only)'],
                        'competition': r['Competition'],
                        'suggested_bid': r['Suggested bid']
                    }
            os.remove(filename)

        return keyword_planner_results_as_a_dict