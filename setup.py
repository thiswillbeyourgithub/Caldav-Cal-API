from setuptools import setup, find_packages

setup(
    name="caldav-cal-api",
    version="0.1.0",
    author="thiswillbeyourgithub",
    description="A Python client for CalDAV calendars (VEVENT), with a CLI.",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    url="https://github.com/thiswillbeyourgithub/Caldav-Cal-API/",
    keywords=[
        "caldav",
        "calendar",
        "vevent",
        "events",
        "ical",
        "icalendar",
        "cli",
        "api",
        "nextcloud",
    ],
    packages=find_packages(include=["caldav_cal_api", "caldav_cal_api.*"]),
    install_requires=[
        "caldav>=1.4.0",
        # icalendar 6.0 is the first release exposing Timezone.from_tzinfo, which
        # to_vcalendar() needs to emit the VTIMEZONE that a DTSTART;TZID= requires.
        # The sibling project relies on icalendar transitively via caldav without
        # declaring it; declaring it here makes the dependency honest.
        "icalendar>=6.0",
        # Used only by EventData.get_occurrences() for RRULE/RDATE/EXDATE expansion.
        "python-dateutil>=2.8",
        "click>=8.1.8",
        "urllib3>=2.5.0",
        "loguru>=0.7.0",
        "platformdirs>=3.0.0",
    ],
    extras_require={
        "dev": [
            "python-dotenv>=1.0.0",
            "black>=25.1.0",
            "twine>=6.1.0",
            "build>=1.2.2.post1",
            "bumpver>=2024.1130",
            "pytest>=8.3.5",
            "pre-commit>=4.2.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "caldav-cal-api=caldav_cal_api.__main__:cli",
        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: GNU Affero General Public License v3",
        "Operating System :: OS Independent",
        "Intended Audience :: Developers",
        "Topic :: Utilities",
    ],
    # The code uses PEP 604 unions (X | None) and builtin generics at runtime, plus
    # zoneinfo, so 3.10 is the honest floor (the sibling claims 3.8 but does not hold).
    python_requires=">=3.10",
)
