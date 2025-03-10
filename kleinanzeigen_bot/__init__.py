"""
Copyright (C) 2022 Sebastian Thomschke and contributors
SPDX-License-Identifier: AGPL-3.0-or-later
"""
import atexit, copy, getopt, importlib.metadata, json, logging, os, re, signal, shutil, sys, textwrap, time, urllib
from collections.abc import Iterable
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Any, Final
from wcmatch import glob

from overrides import overrides
from ruamel.yaml import YAML
from selenium.common.exceptions import ElementClickInterceptedException, NoSuchElementException, TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC

from . import utils, resources, extract  # pylint: disable=W0406
from .utils import abspath, apply_defaults, ensure, is_frozen, pause, pluralize, safe_get, parse_datetime
from .selenium_mixin import SeleniumMixin

# W0406: possibly a bug, see https://github.com/PyCQA/pylint/issues/3933

LOG_ROOT:Final[logging.Logger] = logging.getLogger()
LOG:Final[logging.Logger] = logging.getLogger("kleinanzeigen_bot")
LOG.setLevel(logging.INFO)


class KleinanzeigenBot(SeleniumMixin):

    def __init__(self) -> None:
        super().__init__()

        self.root_url = "https://www.ebay-kleinanzeigen.de"

        self.config:dict[str, Any] = {}
        self.config_file_path = abspath("config.yaml")

        self.categories:dict[str, str] = {}

        self.file_log:logging.FileHandler | None = None
        if is_frozen():
            log_file_basename = os.path.splitext(os.path.basename(sys.executable))[0]
        else:
            log_file_basename = self.__module__
        self.log_file_path:str | None = abspath(f"{log_file_basename}.log")

        self.command = "help"
        self.ads_selector = "due"
        self.delete_old_ads = True
        self.delete_ads_by_title = False

    def __del__(self) -> None:
        if self.file_log:
            LOG_ROOT.removeHandler(self.file_log)

    def get_version(self) -> str:
        return importlib.metadata.version(__package__)

    def run(self, args:list[str]) -> None:
        self.parse_args(args)
        match self.command:
            case "help":
                self.show_help()
            case "version":
                print(self.get_version())
            case "verify":
                self.configure_file_logging()
                self.load_config()
                self.load_ads()
                LOG.info("############################################")
                LOG.info("DONE: No configuration errors found.")
                LOG.info("############################################")
            case "publish":
                self.configure_file_logging()
                self.load_config()
                if ads := self.load_ads():
                    self.create_webdriver_session()
                    self.login()
                    self.publish_ads(ads)
                else:
                    LOG.info("############################################")
                    LOG.info("DONE: No new/outdated ads found.")
                    LOG.info("############################################")
            case "delete":
                self.configure_file_logging()
                self.load_config()
                if ads := self.load_ads():
                    self.create_webdriver_session()
                    self.login()
                    self.delete_ads(ads)
                else:
                    LOG.info("############################################")
                    LOG.info("DONE: No ads to delete found.")
                    LOG.info("############################################")
            case "download":
                self.configure_file_logging()
                # ad IDs depends on selector
                if not (self.ads_selector in {'all', 'new'} or re.compile(r'\d+[,\d+]*').search(self.ads_selector)):
                    LOG.warning('You provided no ads selector. Defaulting to "new".')
                    self.ads_selector = 'new'
                # start session
                self.load_config()
                self.create_webdriver_session()
                self.login()
                self.start_download_routine()  # call correct version of download

            case _:
                LOG.error("Unknown command: %s", self.command)
                sys.exit(2)

    def show_help(self) -> None:
        if is_frozen():
            exe = sys.argv[0]
        elif os.getenv("PDM_PROJECT_ROOT", ""):
            exe = "pdm run app"
        else:
            exe = "python -m kleinanzeigen_bot"

        print(textwrap.dedent(f"""\
            Usage: {exe} COMMAND [OPTIONS]

            Commands:
              publish  - (re-)publishes ads
              verify   - verifies the configuration files
              delete   - deletes ads
              download - downloads one or multiple ads
              --
              help     - displays this help (default command)
              version  - displays the application version

            Options:
              --ads=all|due|new (publish) - specifies which ads to (re-)publish (DEFAULT: due)
                    Possible values:
                    * all: (re-)publish all ads ignoring republication_interval
                    * due: publish all new ads and republish ads according the republication_interval
                    * new: only publish new ads (i.e. ads that have no id in the config file)
              --ads=all|new|<id(s)> (download) - specifies which ads to download (DEFAULT: new)
                    Possible values:
                    * all: downloads all ads from your profile
                    * new: downloads ads from your profile that are not locally saved yet
                    * <id(s)>: provide one or several ads by ID to download, like e.g. "--ads=1,2,3"
              --force           - alias for '--ads=all'
              --keep-old        - don't delete old ads on republication
              --config=<PATH>   - path to the config YAML or JSON file (DEFAULT: ./config.yaml)
              --logfile=<PATH>  - path to the logfile (DEFAULT: ./kleinanzeigen-bot.log)
              -v, --verbose     - enables verbose output - only useful when troubleshooting issues
        """))

    def parse_args(self, args:list[str]) -> None:
        try:
            options, arguments = getopt.gnu_getopt(args[1:], "hv", [
                "ads=",
                "config=",
                "force",
                "help",
                "keep-old",
                "logfile=",
                "verbose"
            ])
        except getopt.error as ex:
            LOG.error(ex.msg)
            LOG.error("Use --help to display available options")
            sys.exit(2)

        for option, value in options:
            match option:
                case "-h" | "--help":
                    self.show_help()
                    sys.exit(0)
                case "--config":
                    self.config_file_path = abspath(value)
                case "--logfile":
                    if value:
                        self.log_file_path = abspath(value)
                    else:
                        self.log_file_path = None
                case "--ads":
                    self.ads_selector = value.strip().lower()
                case "--force":
                    self.ads_selector = "all"
                case "--keep-old":
                    self.delete_old_ads = False
                case "-v" | "--verbose":
                    LOG.setLevel(logging.DEBUG)

        match len(arguments):
            case 0:
                self.command = "help"
            case 1:
                self.command = arguments[0]
            case _:
                LOG.error("More than one command given: %s", arguments)
                sys.exit(2)

    def configure_file_logging(self) -> None:
        if not self.log_file_path:
            return
        if self.file_log:
            return

        LOG.info("Logging to [%s]...", self.log_file_path)
        self.file_log = RotatingFileHandler(filename = self.log_file_path, maxBytes = 10 * 1024 * 1024, backupCount = 10, encoding = "utf-8")
        self.file_log.setLevel(logging.DEBUG)
        self.file_log.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        LOG_ROOT.addHandler(self.file_log)

        LOG.info("App version: %s", self.get_version())

    def load_ads(self, *, ignore_inactive:bool = True, check_id:bool = True) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
        LOG.info("Searching for ad config files...")

        ad_files = set()
        data_root_dir = os.path.dirname(self.config_file_path)
        for file_pattern in self.config["ad_files"]:
            for ad_file in glob.glob(file_pattern, root_dir = data_root_dir, flags = glob.GLOBSTAR | glob.BRACE | glob.EXTGLOB):
                if not str(ad_file).endswith('ad_fields.yaml'):
                    ad_files.add(abspath(ad_file, relative_to = data_root_dir))
        LOG.info(" -> found %s", pluralize("ad config file", ad_files))
        if not ad_files:
            return []

        descr_prefix = self.config["ad_defaults"]["description"]["prefix"] or ""
        descr_suffix = self.config["ad_defaults"]["description"]["suffix"] or ""

        ad_fields = utils.load_dict_from_module(resources, "ad_fields.yaml")
        ads = []
        for ad_file in sorted(ad_files):

            ad_cfg_orig = utils.load_dict(ad_file, "ad")
            ad_cfg = copy.deepcopy(ad_cfg_orig)
            apply_defaults(ad_cfg, self.config["ad_defaults"], ignore = lambda k, _: k == "description", override = lambda _, v: v == "")
            apply_defaults(ad_cfg, ad_fields)

            if ignore_inactive and not ad_cfg["active"]:
                LOG.info(" -> SKIPPED: inactive ad [%s]", ad_file)
                continue

            if self.ads_selector == "new" and ad_cfg["id"] and check_id:
                LOG.info(" -> SKIPPED: ad [%s] is not new. already has an id assigned.", ad_file)
                continue

            if self.ads_selector == "due":
                if ad_cfg["updated_on"]:
                    last_updated_on = parse_datetime(ad_cfg["updated_on"])
                elif ad_cfg["created_on"]:
                    last_updated_on = parse_datetime(ad_cfg["created_on"])
                else:
                    last_updated_on = None

                if last_updated_on:
                    ad_age = datetime.utcnow() - last_updated_on
                    if ad_age.days <= ad_cfg["republication_interval"]:
                        LOG.info(" -> SKIPPED: ad [%s] was last published %d days ago. republication is only required every %s days",
                            ad_file,
                            ad_age.days,
                            ad_cfg["republication_interval"]
                        )
                        continue

            ad_cfg["description"] = descr_prefix + (ad_cfg["description"] or "") + descr_suffix
            ensure(len(ad_cfg["description"]) <= 4000, f"Length of ad description including prefix and suffix exceeds 4000 chars. @ [{ad_file}]")

            # pylint: disable=cell-var-from-loop
            def assert_one_of(path:str, allowed:Iterable[str]) -> None:
                ensure(safe_get(ad_cfg, *path.split(".")) in allowed, f"-> property [{path}] must be one of: {allowed} @ [{ad_file}]")

            def assert_min_len(path:str, minlen:int) -> None:
                ensure(len(safe_get(ad_cfg, *path.split("."))) >= minlen, f"-> property [{path}] must be at least {minlen} characters long @ [{ad_file}]")

            def assert_has_value(path:str) -> None:
                ensure(safe_get(ad_cfg, *path.split(".")), f"-> property [{path}] not specified @ [{ad_file}]")
            # pylint: enable=cell-var-from-loop

            assert_one_of("type", {"OFFER", "WANTED"})
            assert_min_len("title", 10)
            assert_has_value("description")
            assert_one_of("price_type", {"FIXED", "NEGOTIABLE", "GIVE_AWAY", "NOT_APPLICABLE"})
            if ad_cfg["price_type"] == "GIVE_AWAY":
                ensure(not safe_get(ad_cfg, "price"), f"-> [price] must not be specified for GIVE_AWAY ad @ [{ad_file}]")
            elif ad_cfg["price_type"] == "FIXED":
                assert_has_value("price")
            assert_one_of("shipping_type", {"PICKUP", "SHIPPING", "NOT_APPLICABLE"})
            assert_has_value("contact.name")
            assert_has_value("republication_interval")

            if ad_cfg["id"]:
                ad_cfg["id"] = int(ad_cfg["id"])

            if ad_cfg["category"]:
                ad_cfg["category"] = self.categories.get(ad_cfg["category"], ad_cfg["category"])

            if ad_cfg["shipping_costs"]:
                ad_cfg["shipping_costs"] = str(round(utils.parse_decimal(ad_cfg["shipping_costs"]), 2))

            if ad_cfg["images"]:
                images = []
                for image_pattern in ad_cfg["images"]:
                    pattern_images = set()
                    ad_dir = os.path.dirname(ad_file)
                    for image_file in glob.glob(image_pattern, root_dir = ad_dir, flags = glob.GLOBSTAR | glob.BRACE | glob.EXTGLOB):
                        _, image_file_ext = os.path.splitext(image_file)
                        ensure(image_file_ext.lower() in {".gif", ".jpg", ".jpeg", ".png"}, f"Unsupported image file type [{image_file}]")
                        if os.path.isabs(image_file):
                            pattern_images.add(image_file)
                        else:
                            pattern_images.add(abspath(image_file, relative_to = ad_file))
                    images.extend(sorted(pattern_images))
                ensure(images or not ad_cfg["images"], f"No images found for given file patterns {ad_cfg['images']} at {ad_dir}")
                ad_cfg["images"] = list(dict.fromkeys(images))

            ads.append((
                ad_file,
                ad_cfg,
                ad_cfg_orig
            ))

        LOG.info("Loaded %s", pluralize("ad", ads))
        return ads

    def load_config(self) -> None:
        config_defaults = utils.load_dict_from_module(resources, "config_defaults.yaml")
        config = utils.load_dict_if_exists(self.config_file_path, "config")

        if config is None:
            LOG.warning("Config file %s does not exist. Creating it with default values...", self.config_file_path)
            utils.save_dict(self.config_file_path, config_defaults)
            config = {}

        self.config = apply_defaults(config, config_defaults)

        self.categories = utils.load_dict_from_module(resources, "categories.yaml", "categories")
        if self.config["categories"]:
            self.categories.update(self.config["categories"])
        LOG.info(" -> found %s", pluralize("category", self.categories))

        ensure(self.config["login"]["username"], f"[login.username] not specified @ [{self.config_file_path}]")
        ensure(self.config["login"]["password"], f"[login.password] not specified @ [{self.config_file_path}]")

        self.browser_config.arguments = self.config["browser"]["arguments"]
        self.browser_config.binary_location = self.config["browser"]["binary_location"]
        self.browser_config.extensions = [abspath(item, relative_to = self.config_file_path) for item in self.config["browser"]["extensions"]]
        self.browser_config.use_private_window = self.config["browser"]["use_private_window"]
        if self.config["browser"]["user_data_dir"]:
            self.browser_config.user_data_dir = abspath(self.config["browser"]["user_data_dir"], relative_to = self.config_file_path)
        self.browser_config.profile_name = self.config["browser"]["profile_name"]

    def login(self) -> None:
        LOG.info("Logging in as [%s]...", self.config["login"]["username"])
        self.web_open(f"{self.root_url}/m-einloggen.html?targetUrl=/")

        # accept privacy banner
        try:
            self.web_click(By.ID, "gdpr-banner-accept")
        except NoSuchElementException:
            pass

        self.web_input(By.ID, "login-email", self.config["login"]["username"])
        self.web_input(By.ID, "login-password", self.config["login"]["password"])

        self.handle_captcha_if_present("login-recaptcha", "but DON'T click 'Einloggen'.")

        self.web_click(By.ID, "login-submit")

        try:
            self.web_find(By.ID, "new-device-login", 4)
            LOG.warning("############################################")
            LOG.warning("# Device verification message detected. Use the 'Login bestätigen' URL from the mentioned e-mail into the same browser tab.")
            LOG.warning("############################################")
            input("Press ENTER when done...")
        except NoSuchElementException:
            pass

    def handle_captcha_if_present(self, captcha_element_id:str, msg:str) -> None:
        try:
            self.web_click(By.XPATH, f"//*[@id='{captcha_element_id}']")
        except NoSuchElementException:
            return

        LOG.warning("############################################")
        LOG.warning("# Captcha present! Please solve and close the captcha, %s", msg)
        LOG.warning("############################################")
        self.webdriver.switch_to.frame(self.web_find(By.CSS_SELECTOR, f"#{captcha_element_id} iframe"))
        self.web_await(lambda _: self.webdriver.find_element(By.ID, "recaptcha-anchor").get_attribute("aria-checked") == "true", timeout = 5 * 60)
        self.webdriver.switch_to.default_content()

    def delete_ads(self, ad_cfgs:list[tuple[str, dict[str, Any], dict[str, Any]]]) -> None:
        count = 0

        for (ad_file, ad_cfg, _) in ad_cfgs:
            count += 1
            LOG.info("Processing %s/%s: '%s' from [%s]...", count, len(ad_cfgs), ad_cfg["title"], ad_file)
            self.delete_ad(ad_cfg)
            pause(2000, 4000)

        LOG.info("############################################")
        LOG.info("DONE: Deleting %s", pluralize("ad", count))
        LOG.info("############################################")

    def delete_ad(self, ad_cfg: dict[str, Any]) -> bool:
        LOG.info("Deleting ad '%s' if already present...", ad_cfg["title"])

        self.web_open(f"{self.root_url}/m-meine-anzeigen.html")
        csrf_token_elem = self.web_find(By.XPATH, "//meta[@name='_csrf']")
        csrf_token = csrf_token_elem.get_attribute("content")

        if self.delete_ads_by_title:
            published_ads = json.loads(self.web_request(f"{self.root_url}/m-meine-anzeigen-verwalten.json?sort=DEFAULT")["content"])["ads"]

            for published_ad in published_ads:
                published_ad_id = int(published_ad.get("id", -1))
                published_ad_title = published_ad.get("title", "")
                if ad_cfg["id"] == published_ad_id or ad_cfg["title"] == published_ad_title:
                    LOG.info(" -> deleting %s '%s'...", published_ad_id, published_ad_title)
                    self.web_request(
                        url = f"{self.root_url}/m-anzeigen-loeschen.json?ids={published_ad_id}",
                        method = "POST",
                        headers = {"x-csrf-token": csrf_token}
                    )
        elif ad_cfg["id"]:
            self.web_request(
                url = f"{self.root_url}/m-anzeigen-loeschen.json?ids={ad_cfg['id']}",
                method = "POST",
                headers = {"x-csrf-token": csrf_token},
                valid_response_codes = [200, 404]
            )

        pause(1500, 3000)
        ad_cfg["id"] = None
        return True

    def publish_ads(self, ad_cfgs:list[tuple[str, dict[str, Any], dict[str, Any]]]) -> None:
        count = 0

        for (ad_file, ad_cfg, ad_cfg_orig) in ad_cfgs:
            count += 1
            LOG.info("Processing %s/%s: '%s' from [%s]...", count, len(ad_cfgs), ad_cfg["title"], ad_file)
            self.publish_ad(ad_file, ad_cfg, ad_cfg_orig)
            self.web_await(lambda _: self.webdriver.find_element(By.ID, "checking-done").is_displayed(), timeout = 5 * 60)

        LOG.info("############################################")
        LOG.info("DONE: (Re-)published %s", pluralize("ad", count))
        LOG.info("############################################")

    def publish_ad(self, ad_file:str, ad_cfg: dict[str, Any], ad_cfg_orig: dict[str, Any]) -> None:
        self.assert_free_ad_limit_not_reached()

        if self.delete_old_ads:
            self.delete_ad(ad_cfg)

        LOG.info("Publishing ad '%s'...", ad_cfg["title"])

        if LOG.isEnabledFor(logging.DEBUG):
            LOG.debug(" -> effective ad meta:")
            YAML().dump(ad_cfg, sys.stdout)

        self.web_open(f"{self.root_url}/p-anzeige-aufgeben-schritt2.html")

        if ad_cfg["type"] == "WANTED":
            self.web_click(By.ID, "adType2")

        #############################
        # set title
        #############################
        self.web_input(By.ID, "postad-title", ad_cfg["title"])

        #############################
        # set category
        #############################
        self.__set_category(ad_file, ad_cfg)

        #############################
        # set shipping type/options/costs
        #############################
        if ad_cfg["shipping_type"] == "PICKUP":
            try:
                self.web_click(By.XPATH, '//*[contains(@class, "ShippingPickupSelector")]//label[text()[contains(.,"Nur Abholung")]]/input[@type="radio"]')
            except NoSuchElementException as ex:
                LOG.debug(ex, exc_info = True)
        elif ad_cfg["shipping_options"]:
            self.__set_shipping_options(ad_cfg)
        elif ad_cfg["shipping_costs"]:
            try:
                self.web_click(By.XPATH, '//*[contains(@class, "ShippingOption")]//input[@type="radio"]')
                self.web_click(By.XPATH, '//*[contains(@class, "CarrierOptionsPopup")]//*[contains(@class, "IndividualPriceSection")]//input[@type="checkbox"]')
                self.web_input(By.XPATH, '//*[contains(@class, "IndividualShippingInput")]//input[@type="text"]',
                               str.replace(ad_cfg["shipping_costs"], ".", ","))
                self.web_click(By.XPATH, '//*[contains(@class, "ReactModalPortal")]//button[.//*[text()[contains(.,"Weiter")]]]')
            except NoSuchElementException as ex:
                LOG.debug(ex, exc_info = True)

        #############################
        # set price
        #############################
        price_type = ad_cfg["price_type"]
        if price_type != "NOT_APPLICABLE":
            self.web_select(By.XPATH, "//select[@id='price-type-react' or @id='micro-frontend-price-type' or @id='priceType']", price_type)
            if safe_get(ad_cfg, "price"):
                self.web_input(By.XPATH, "//input[@id='post-ad-frontend-price' or @id='micro-frontend-price' or @id='pstad-price']", ad_cfg["price"])

        #############################
        # set sell_directly
        #############################
        sell_directly = ad_cfg["sell_directly"]
        try:
            if sell_directly and ad_cfg["shipping_type"] == "SHIPPING" and ad_cfg["shipping_options"] and price_type in {"FIXED", "NEGOTIABLE"}:
                if not self.webdriver.find_element(By.ID, "buy-now-toggle").is_selected():
                    self.web_click(By.XPATH, '//*[contains(@class, "BuyNowSection")]//span[contains(@class, "Toggle--Slider")]')
            elif self.webdriver.find_element(By.ID, "buy-now-toggle").is_selected():
                self.web_click(By.XPATH, '//*[contains(@class, "BuyNowSection")]//span[contains(@class, "Toggle--Slider")]')
        except NoSuchElementException as ex:
            LOG.debug(ex, exc_info = True)

        #############################
        # set description
        #############################
        self.web_execute("document.querySelector('#pstad-descrptn').value = `" + ad_cfg["description"].replace("`", "'") + "`")

        #############################
        # set contact zipcode
        #############################
        if ad_cfg["contact"]["zipcode"]:
            self.web_input(By.ID, "pstad-zip", ad_cfg["contact"]["zipcode"])

        #############################
        # set contact street
        #############################
        if ad_cfg["contact"]["street"]:
            try:
                if not self.webdriver.find_element(By.ID, "pstad-street").is_enabled():
                    self.webdriver.find_element(By.ID, "addressVisibility").click()
                    pause(2000)
            except NoSuchElementException:
                # ignore
                pass
            self.web_input(By.ID, "pstad-street", ad_cfg["contact"]["street"])

        #############################
        # set contact name
        #############################
        if ad_cfg["contact"]["name"]:
            self.web_input(By.ID, "postad-contactname", ad_cfg["contact"]["name"])

        #############################
        # set contact phone
        #############################
        if ad_cfg["contact"]["phone"]:
            if self.webdriver.find_element(By.ID, "postad-phonenumber").is_displayed():
                try:
                    if not self.webdriver.find_element(By.ID, "postad-phonenumber").is_enabled():
                        self.webdriver.find_element(By.ID, "phoneNumberVisibility").click()
                        pause(2000)
                except NoSuchElementException:
                    # ignore
                    pass
                self.web_input(By.ID, "postad-phonenumber", ad_cfg["contact"]["phone"])

        #############################
        # upload images
        #############################
        self.__upload_images(ad_cfg)

        #############################
        # submit
        #############################
        self.handle_captcha_if_present("postAd-recaptcha", "but DON'T click 'Anzeige aufgeben'.")
        try:
            self.web_click(By.ID, "pstad-submit")
        except NoSuchElementException:
            # https://github.com/Second-Hand-Friends/kleinanzeigen-bot/issues/40
            self.web_click(By.XPATH, "//fieldset[@id='postad-publish']//*[contains(text(),'Anzeige aufgeben')]")
            self.web_click(By.ID, "imprint-guidance-submit")

        self.web_await(EC.url_contains("p-anzeige-aufgeben-bestaetigung.html?adId="), 20)

        ad_cfg_orig["updated_on"] = datetime.utcnow().isoformat()
        if not ad_cfg["created_on"] and not ad_cfg["id"]:
            ad_cfg_orig["created_on"] = ad_cfg_orig["updated_on"]

        # extract the ad id from the URL's query parameter
        current_url_query_params = urllib.parse.parse_qs(urllib.parse.urlparse(self.webdriver.current_url).query)
        ad_id = int(current_url_query_params.get("adId", None)[0])
        ad_cfg_orig["id"] = ad_id

        LOG.info(" -> SUCCESS: ad published with ID %s", ad_id)

        utils.save_dict(ad_file, ad_cfg_orig)

    def __set_category(self, ad_file:str, ad_cfg: dict[str, Any]):
        # click on something to trigger automatic category detection
        self.web_click(By.ID, "pstad-descrptn")

        try:
            self.web_find(By.XPATH, "//*[@id='postad-category-path'][text()]")
            is_category_auto_selected = True
        except NoSuchElementException:
            is_category_auto_selected = False

        if ad_cfg["category"]:
            utils.pause(2000)  # workaround for https://github.com/Second-Hand-Friends/kleinanzeigen-bot/issues/39
            self.web_click(By.ID, "pstad-lnk-chngeCtgry")
            self.web_find(By.ID, "postad-step1-sbmt")

            category_url = f"{self.root_url}/p-kategorie-aendern.html#?path={ad_cfg['category']}"
            self.web_open(category_url)
            self.web_click(By.XPATH, "//*[@id='postad-step1-sbmt']/button")
        else:
            ensure(is_category_auto_selected, f"No category specified in [{ad_file}] and automatic category detection failed")

        if ad_cfg["special_attributes"]:
            LOG.debug('Found %i special attributes', len(ad_cfg["special_attributes"]))
            for special_attribute_key, special_attribute_value in ad_cfg["special_attributes"].items():
                LOG.debug("Setting special attribute [%s] to [%s]...", special_attribute_key, special_attribute_value)
                try:
                    self.web_select(By.XPATH, f"//select[@id='{special_attribute_key}']", special_attribute_value)
                except WebDriverException:
                    LOG.debug("Attribute field '%s' is not of kind dropdown, trying to input as plain text...", special_attribute_key)
                    try:
                        self.web_input(By.ID, special_attribute_key, special_attribute_value)
                    except WebDriverException:
                        LOG.debug("Attribute field '%s' is not of kind plain text, trying to input as radio button...", special_attribute_key)
                        try:
                            self.web_click(By.XPATH, f"//*[@id='{special_attribute_key}']/option[@value='{special_attribute_value}']")
                        except WebDriverException as ex:
                            LOG.debug("Attribute field '%s' is not of kind radio button.", special_attribute_key)
                            raise NoSuchElementException(f"Failed to set special attribute [{special_attribute_key}]") from ex
                LOG.debug("Successfully set attribute field [%s] to [%s]...", special_attribute_key, special_attribute_value)

    def __set_shipping_options(self, ad_cfg: dict[str, Any]) -> None:
        try:
            shipping_option_mapping = {
                "DHL_2": ("Klein", "Paket 2 kg"),
                "Hermes_Päckchen": ("Klein", "Päckchen"),
                "Hermes_S": ("Klein", "S-Paket"),
                "DHL_5": ("Mittel", "Paket 5 kg"),
                "Hermes_M": ("Mittel", "M-Paket"),
                "DHL_10": ("Mittel", "Paket 10 kg"),
                "DHL_31,5": ("Groß", "Paket 31,5 kg"),
                "Hermes_L": ("Groß", "L-Paket"),
            }
            try:
                mapped_shipping_options = [shipping_option_mapping[option] for option in ad_cfg["shipping_options"]]
                shipping_sizes, shipping_packages = zip(*mapped_shipping_options)
            except KeyError as ex:
                raise KeyError(f"Unknown shipping option(s), please refer to the documentation/README: {ad_cfg['shipping_options']}") from ex

            unique_shipping_sizes = set(shipping_sizes)
            if len(unique_shipping_sizes) > 1:
                raise ValueError("You can only specify shipping options for one package size!")

            shipping_size, = unique_shipping_sizes
            self.web_click(By.XPATH, f'//*[contains(@class, "ShippingOption")]//input[@type="radio" and @data-testid="{shipping_size}"]')

            for shipping_package in shipping_packages:
                self.web_click(
                    By.XPATH,
                    '//*[contains(@class, "CarrierOptionsPopup")]'
                    '//*[contains(@class, "CarrierOption")]'
                    f'//input[@type="checkbox" and @data-testid="{shipping_package}"]'
                )

            self.web_click(By.XPATH, '//*[contains(@class, "ReactModalPortal")]//button[.//*[text()[contains(.,"Weiter")]]]')
        except NoSuchElementException as ex:
            LOG.debug(ex, exc_info = True)

    def __upload_images(self, ad_cfg: dict[str, Any]):
        LOG.info(" -> found %s", pluralize("image", ad_cfg["images"]))
        image_upload = self.web_find(By.XPATH, "//input[@type='file']")

        def count_uploaded_images() -> int:
            return len(self.webdriver.find_elements(By.CLASS_NAME, "imagebox-new-thumbnail"))

        for image in ad_cfg["images"]:
            LOG.info(" -> uploading image [%s]", image)
            previous_uploaded_images_count = count_uploaded_images()
            image_upload.send_keys(image)
            start_at = time.time()
            while previous_uploaded_images_count == count_uploaded_images() and time.time() - start_at < 60:
                print(".", end = "", flush = True)
                time.sleep(1)
            print(flush = True)

            ensure(previous_uploaded_images_count < count_uploaded_images(), f"Couldn't upload image [{image}] within 60 seconds")
            LOG.debug("   => uploaded image within %i seconds", time.time() - start_at)
            pause(2000)

    def assert_free_ad_limit_not_reached(self) -> None:
        try:
            self.web_find(By.XPATH, '/html/body/div[1]/form/fieldset[6]/div[1]/header')
            raise AssertionError(f"Cannot publish more ads. The monthly limit of free ads of account {self.config['login']['username']} is reached.")
        except NoSuchElementException:
            pass

    @overrides
    def web_open(self, url:str, timeout:float = 15, reload_if_already_open:bool = False) -> None:
        start_at = time.time()
        super().web_open(url, timeout, reload_if_already_open)
        pause(2000)

        # reload the page until no fullscreen ad is displayed anymore
        while True:
            try:
                self.web_find(By.XPATH, "/html/body/header[@id='site-header']", 2)
                return
            except NoSuchElementException as ex:
                elapsed = time.time() - start_at
                if elapsed < timeout:
                    super().web_open(url, timeout - elapsed, True)
                else:
                    raise TimeoutException("Loading page failed, it still shows fullscreen ad.") from ex

    def navigate_to_ad_page(self, id_:int | None = None, url:str | None = None) -> bool:
        """
        Navigates to an ad page specified with an ad ID; or alternatively by a given URL.

        :param id_: if provided (and no url given), the ID is used to search for the ad to navigate to
        :param url: if given, this URL is used instead of an id to find the ad page
        :return: whether the navigation to the ad page was successful
        """
        if not (id_ or url):
            raise UserWarning('This function needs either the "id_" or "url" parameter given!')
        if url:
            self.webdriver.get(url)  # navigate to URL directly given
        else:
            # enter the ad ID into the search bar
            self.web_input(By.XPATH, '//*[@id="site-search-query"]', str(id_))
            # navigate to ad page and wait
            submit_button = self.webdriver.find_element(By.XPATH, '//*[@id="site-search-submit"]')
            self.web_await(EC.element_to_be_clickable(submit_button), 15)
            try:
                submit_button.click()
            except ElementClickInterceptedException:  # sometimes: special banner might pop up and intercept
                LOG.warning('Waiting for unexpected element to close...')
                pause(6000, 10000)
                submit_button.click()
        pause(1000, 2000)

        # handle the case that invalid ad ID given
        if self.webdriver.current_url.endswith('k0'):
            LOG.error('There is no ad under the given ID.')
            return False
        try:  # close (warning) popup, if given
            self.webdriver.find_element(By.CSS_SELECTOR, '#vap-ovrly-secure')
            LOG.warning('A popup appeared.')
            close_button = self.webdriver.find_element(By.CLASS_NAME, 'mfp-close')
            close_button.click()
            time.sleep(1)
        except NoSuchElementException:
            print('(no popup)')
        return True

    def download_images_from_ad_page(self, directory:str, ad_id:int, logger:logging.Logger) -> list[str]:
        """
        Downloads all images of an ad.

        :param directory: the path of the directory created for this ad
        :param ad_id: the ID of the ad to download the images from
        :param logger: an initialized logger
        :return: the relative paths for all downloaded images
        """

        n_images:int
        img_paths = []
        try:
            image_box = self.webdriver.find_element(By.CSS_SELECTOR, '.galleryimage-large')

            # if gallery image box exists, proceed with image fetching
            n_images = 1

            # determine number of images (1 ... N)
            next_button = None
            try:  # check if multiple images given
                # edge case: 'Virtueller Rundgang' div could be found by same CSS class
                element_candidates = image_box.find_elements(By.CSS_SELECTOR, '.galleryimage--info')
                image_counter = element_candidates[-1]
                n_images = int(image_counter.text[2:])
                logger.info('Found %d images.', n_images)
                next_button = self.webdriver.find_element(By.CSS_SELECTOR, '.galleryimage--navigation--next')
            except (NoSuchElementException, IndexError):
                logger.info('Only one image found.')

            # download all images from box
            img_element = image_box.find_element(By.XPATH, './/div[1]/img')
            img_fn_prefix = 'ad_' + str(ad_id) + '__img'

            img_nr = 1
            dl_counter = 0
            while img_nr <= n_images:  # scrolling + downloading
                current_img_url = img_element.get_attribute('src')  # URL of the image
                file_ending = current_img_url.split('.')[-1].lower()
                img_path = directory + '/' + img_fn_prefix + str(img_nr) + '.' + file_ending
                if current_img_url.startswith('https'):  # verify https (for Bandit linter)
                    urllib.request.urlretrieve(current_img_url, img_path)  # nosec B310
                dl_counter += 1
                img_paths.append(img_path.split('/')[-1])

                # scroll to next image (if exists)
                if img_nr < n_images:
                    try:
                        # click next button, wait, and reestablish reference
                        next_button.click()
                        self.web_await(lambda _: EC.staleness_of(img_element))
                        new_div = self.webdriver.find_element(By.CSS_SELECTOR, f'div.galleryimage-element:nth-child({img_nr + 1})')
                        img_element = new_div.find_element(By.XPATH, './/img')
                    except NoSuchElementException:
                        logger.error('NEXT button in image gallery somehow missing, abort image fetching.')
                        break
                img_nr += 1
            logger.info('Downloaded %d image(s).', dl_counter)

        except NoSuchElementException:  # some ads do not require images
            logger.warning('No image area found. Continue without downloading images.')

        return img_paths

    def extract_ad_page_info(self, directory:str, id_:int) -> dict:
        """
        Extracts all necessary information from an ad´s page.

        :param directory: the path of the ad´s previously created directory
        :param id_: the ad ID, already extracted by a calling function
        :return: a dictionary with the keys as given in an ad YAML, and their respective values
        """
        info = {'active': True}

        # extract basic info
        if 's-anzeige' in self.webdriver.current_url:
            o_type = 'OFFER'
        else:
            o_type = 'WANTED'
        info['type'] = o_type
        title:str = self.webdriver.find_element(By.CSS_SELECTOR, '#viewad-title').text
        LOG.info('Extracting information from ad with title \"%s\"', title)
        info['title'] = title
        descr:str = self.webdriver.find_element(By.XPATH, '//*[@id="viewad-description-text"]').text
        info['description'] = descr

        extractor = extract.AdExtractor(self.webdriver)

        # extract category
        info['category'] = extractor.extract_category_from_ad_page()

        # get special attributes
        info['special_attributes'] = extractor.extract_special_attributes_from_ad_page()

        # process pricing
        info['price'], info['price_type'] = extractor.extract_pricing_info_from_ad_page()

        # process shipping
        info['shipping_type'], info['shipping_costs'], info['shipping_options'] = extractor.extract_shipping_info_from_ad_page()
        info['sell_directly'] = extractor.extract_sell_directly_from_ad_page()

        # fetch images
        info['images'] = self.download_images_from_ad_page(directory, id_, LOG)

        # process address
        info['contact'] = extractor.extract_contact_from_ad_page()

        # process meta info
        info['republication_interval'] = 7  # a default value for downloaded ads
        info['id'] = id_

        try:  # try different locations known for creation date element
            creation_date = self.webdriver.find_element(By.XPATH, '/html/body/div[1]/div[2]/div/section[2]/section/section/article/div[3]/div[2]/div[2]/'
                                                                  'div[1]/span').text
        except NoSuchElementException:
            creation_date = self.webdriver.find_element(By.CSS_SELECTOR, '#viewad-extra-info > div:nth-child(1) > span:nth-child(2)').text

        # convert creation date to ISO format
        created_parts = creation_date.split('.')
        creation_date = created_parts[2] + '-' + created_parts[1] + '-' + created_parts[0] + ' 00:00:00'
        creation_date = datetime.fromisoformat(creation_date).isoformat()
        info['created_on'] = creation_date
        info['updated_on'] = None  # will be set later on

        return info

    def download_ad_page(self, id_:int):
        """
        Downloads an ad to a specific location, specified by config and ad ID.
        NOTE: Requires that the driver session currently is on the ad page.

        :param id_: the ad ID
        """

        # create sub-directory for ad(s) to download (if necessary):
        relative_directory = 'downloaded-ads'
        # make sure configured base directory exists
        if not os.path.exists(relative_directory) or not os.path.isdir(relative_directory):
            os.mkdir(relative_directory)
            LOG.info('Created ads directory at /%s.', relative_directory)

        new_base_dir = os.path.join(relative_directory, f'ad_{id_}')
        if os.path.exists(new_base_dir):
            LOG.info('Deleting current folder of ad...')
            shutil.rmtree(new_base_dir)
        os.mkdir(new_base_dir)
        LOG.info('New directory for ad created at %s.', new_base_dir)

        # call extraction function
        info = self.extract_ad_page_info(new_base_dir, id_)
        ad_file_path = new_base_dir + '/' + f'ad_{id_}.yaml'
        utils.save_dict(ad_file_path, info)

    def start_download_routine(self):
        """
        Determines which download mode was chosen with the arguments, and calls the specified download routine.
        This downloads either all, only unsaved (new), or specific ads given by ID.
        """

        # use relevant download routine
        if self.ads_selector in {'all', 'new'}:  # explore ads overview for these two modes
            LOG.info('Scanning your ad overview...')
            ext = extract.AdExtractor(self.webdriver)
            refs = ext.extract_own_ads_references()
            LOG.info('%d ads were found!', len(refs))

            if self.ads_selector == 'all':  # download all of your adds
                LOG.info('Start fetch task for all your ads!')

                success_count = 0
                # call download function for each ad page
                for ref in refs:
                    ref_ad_id: int = utils.extract_ad_id_from_ad_link(ref)
                    if self.navigate_to_ad_page(url = ref):
                        self.download_ad_page(ref_ad_id)
                        success_count += 1
                LOG.info("%d of %d ads were downloaded from your profile.", success_count, len(refs))

            elif self.ads_selector == 'new':  # download only unsaved ads
                # determine ad IDs from links
                ref_ad_ids = [utils.extract_ad_id_from_ad_link(r) for r in refs]
                ref_pairs = list(zip(refs, ref_ad_ids))

                # check which ads already saved
                saved_ad_ids = []
                ads = self.load_ads(ignore_inactive=False, check_id=False)  # do not skip because of existing IDs
                for ad_ in ads:
                    ad_id = int(ad_[2]['id'])
                    saved_ad_ids.append(ad_id)

                LOG.info('Start fetch task for your unsaved ads!')
                new_count = 0
                for ref_pair in ref_pairs:
                    # check if ad with ID already saved
                    id_: int = ref_pair[1]
                    if id_ in saved_ad_ids:
                        LOG.info('The ad with id %d has already been saved.', id_)
                        continue

                    if self.navigate_to_ad_page(url = ref_pair[0]):
                        self.download_ad_page(id_)
                        new_count += 1
                LOG.info('%d new ad(s) were downloaded from your profile.', new_count)

        elif re.compile(r'\d+[,\d+]*').search(self.ads_selector):  # download ad(s) with specific id(s)
            ids = [int(n) for n in self.ads_selector.split(',')]
            LOG.info('Start fetch task for the ad(s) with the id(s):')
            LOG.info(' | '.join([str(id_) for id_ in ids]))

            for id_ in ids:  # call download routine for every id
                exists = self.navigate_to_ad_page(id_)
                if exists:
                    self.download_ad_page(id_)
                    LOG.info('Downloaded ad with id %d', id_)
                else:
                    LOG.error('The page with the id %d does not exist!', id_)


#############################
# main entry point
#############################
def main(args:list[str]) -> None:
    if "version" not in args:
        print(textwrap.dedent(r"""
         _    _      _                           _                       _           _
        | | _| | ___(_)_ __   __ _ _ __  _______(_) __ _  ___ _ __      | |__   ___ | |_
        | |/ / |/ _ \ | '_ \ / _` | '_ \|_  / _ \ |/ _` |/ _ \ '_ \ ____| '_ \ / _ \| __|
        |   <| |  __/ | | | | (_| | | | |/ /  __/ | (_| |  __/ | | |____| |_) | (_) | |_
        |_|\_\_|\___|_|_| |_|\__,_|_| |_/___\___|_|\__, |\___|_| |_|    |_.__/ \___/ \__|
                                                   |___/
                                 https://github.com/Second-Hand-Friends/kleinanzeigen-bot
        """), flush = True)

    utils.configure_console_logging()

    signal.signal(signal.SIGINT, utils.on_sigint)  # capture CTRL+C
    sys.excepthook = utils.on_exception
    atexit.register(utils.on_exit)

    KleinanzeigenBot().run(args)


if __name__ == "__main__":
    utils.configure_console_logging()
    LOG.error("Direct execution not supported. Use 'pdm run app'")
    sys.exit(1)
