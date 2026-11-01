"""Standard-library modules the GENERATED game is allowed to use.

PyInstaller only packs modules that our own code imports. The generated game is
written later, by the model, so we import its likely needs here: that makes the
.exe able to run the generated tests (--run-tests) and the game itself
(--run-script) on a machine that has no Python installed."""
# flake8: noqa
import abc, argparse, collections, contextlib, copy, csv, dataclasses, datetime, enum, fractions
import functools, hashlib, itertools, json, logging, math, os, pathlib, random, re, sqlite3
import statistics, string, sys, textwrap, time, typing, uuid
import unittest, unittest.mock
import tkinter, tkinter.ttk, tkinter.messagebox, tkinter.simpledialog, tkinter.filedialog, tkinter.font
