import streamlit as st
import pandas as pd
import gc

st.set_page_config(layout="wide", initial_sidebar_state="expanded")

gc.collect()

st.title("Visual Interactive Simulation (VIS) - Demonstration")

st.markdown(
    """
This streamlit app demonstrates the use of a visual interactive simulation (VIS) package for showing the position of queues and resource utilisation in a manner understandable to stakeholders.

It is also valuable for developers, as the functioning of the simulation can be more easily monitored.

It is designed for integration with simpy - however, in theory, it could be integrated with different simulation packages in Python or other languages.

Please use the tabs on the left hand side to view different examples of how this package can be used.
    """
)