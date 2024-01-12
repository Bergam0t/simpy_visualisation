'''

Classes and functions for the scheduling example lab.
This is used to build a model of the queuing and scheduling
at a mental health assessment network across in Devon

'''
import pandas as pd
import numpy as np
import itertools
import simpy
import random

from examples.distribution_classes import Bernoulli, Discrete, Poisson, Lognormal

def generate_seed_vector(one_seed_to_rule_them_all=42, size=20):
    '''
    Return a controllable numpy array
    of integer seeds to use in simulation model.

    Values are between 1000 and 10^10

    Params:
    ------
    one_seed_to_rule_them_all: int, optional (default=42)
        seed to produce the seed vector

    size: int, optional (default=20)
        length of seed vector
    '''
    rng = np.random.default_rng(seed=one_seed_to_rule_them_all)
    return rng.integers(low=1000, high=10**10, size=size)

ANNUAL_DEMAND = 1500
LOW_PRIORITY_MIN_WAIT = 7
HIGH_PRIORITY_MIN_WAIT = 2

PROP_HIGH_PRORITY= 0.15
PROP_CARVE_OUT = 0.15

# What proportion of people initially graded as *high* priority
# go on to have ongoing appointments?
PROP_HIGH_PRIORITY_ONGOING_APPOINTMENTS = 0.95

# What proportion of people initially graded as *low* priority
# go on to have ongoing appointments?
PROP_LOW_PRIORITY_ONGOING_APPOINTMENTS = 0.8

# What proportion of people initially graded as *high*
# priority go on to have high intensity therapy?
PROP_HIGH_PRIORITY_HIGH_INTENSITY = 0.7
# What proportion of people initially graded as *low*
# priority go on to have high intensity therapy?
PROP_LOW_PRIORITY_HIGH_INTENSITY = 0.2

MEAN_FOLLOW_UPS_HIGH_INTENSITY = 10
MEAN_FOLLOW_UPS_LOW_INTENSITY = 6

LOW_INTENSITY_FOLLOW_UP_TARGET_INTERVAL = 14
HIGH_INTENSITY_FOLLOW_UP_TARGET_INTERVAL = 7

#targets in working days
TARGET_HIGH = 5
TARGET_LOW = 20

# MAXIMUM WAIT FOR APPOINTMENT FROM BOOKING
# This will be used to decide whether to book, or whether to wait until an
# appointment less far in the future is available
BOOKING_TIME_THRESHOLD = 4*7


class Clinic():
    '''
    A clinic has a probability of refering patients
    to another service after triage.
    '''
    def __init__(self, prob_referral_out, random_seed=None):

        #prob patient is referred to another service
        self.prob_referral_out = prob_referral_out
        self.ref_out_dist = Bernoulli(prob_referral_out, random_seed)

class Scenario():
    '''
    Arguments represent a configuration of the simulation model.
    '''
    def __init__(self, run_length,
                 warm_up=0.0,
                 prop_carve_out=0.15,
                 demand_file=None, slots_file=None,
                 pooling_file=None, existing_caseload_file=None,
                 annual_demand=ANNUAL_DEMAND,
                 prop_high_priority=PROP_HIGH_PRORITY,
                 seeds=None):

        if seeds is None:
            self.seeds = [None for i in range(100)]
        else:
            self.seeds = seeds

        #use default files?
        if pooling_file is None:
            pooling_file = pd.read_csv('examples/ex_5_community_follow_up/data/partial_pooling.csv')

        if demand_file is None:
            demand_file = pd.read_csv('examples/ex_5_community_follow_up/data/referrals.csv')

        if slots_file is None:
            slots_file = pd.read_csv('examples/ex_5_community_follow_up/data/shifts.csv')

        if existing_caseload_file is None:
            existing_caseload_file = pd.read_csv('examples/ex_5_community_follow_up/data/caseload.csv')

        #useful if you want to record anything during a model run.
        self.debug = []

        #run length and warm up period
        self.run_length = run_length
        self.warm_up_period = warm_up

        #should we pool clinics?
        self.pooling = True

        #proportion of carve out used
        self.prop_carve_out = prop_carve_out

        #input data from files
        self.clinic_demand = demand_file
        self.weekly_slots = slots_file
        self.pooling_np = pooling_file.to_numpy().T[1:].T
        self.existing_caseload = existing_caseload_file.iloc[0,]

        #These represent the 'diaries' of bookings

        # 1. carve out
        self.carve_out_slots = self.create_carve_out(run_length,
                                                     self.weekly_slots)

        # 2. available slots and one for the bookings.
        self.available_slots = self.create_slots(self.run_length,
                                                 self.weekly_slots)

        # 3. the bookings which can be used to calculate slot utilisation
        self.bookings = self.create_bookings(self.run_length,
                                             len(self.weekly_slots.columns))

        #sampling distributions
        # Arrival rate of patients to the service
        self.arrival_dist = Poisson(annual_demand / 52 / 5,
                                    random_seed=self.seeds[0])
        # Initial priority setting for assessment
        self.priority_dist = Bernoulli(prop_high_priority,
                                       random_seed=self.seeds[1])

        # Determining whether people will have follow-up appointments
        self.follow_up_dist_high_priority = Bernoulli(
            PROP_HIGH_PRIORITY_ONGOING_APPOINTMENTS,
            random_seed=self.seeds[2])
        self.follow_up_dist_low_priority = Bernoulli(
            PROP_LOW_PRIORITY_ONGOING_APPOINTMENTS,
            random_seed=self.seeds[3])

        # Setting intensity (frequency) of follow-up appointments
        self.intensity_dist_high_priority = Bernoulli(
            PROP_HIGH_PRIORITY_HIGH_INTENSITY,
            random_seed=self.seeds[4])
        self.intensity_dist_low_priority = Bernoulli(
            PROP_LOW_PRIORITY_HIGH_INTENSITY,
            random_seed=self.seeds[5])

        # Setting number of follow up appointments - high intensity
        self.num_follow_up_dist_high_intensity = Lognormal(
            mean=MEAN_FOLLOW_UPS_HIGH_INTENSITY,
            stdev=6,
            random_seed=self.seeds[6]
            )

        self.num_follow_up_dist_low_intensity = Lognormal(
            mean=MEAN_FOLLOW_UPS_LOW_INTENSITY,
            stdev=3,
            random_seed=self.seeds[7]
            )


        #create a distribution for sampling a patients local clinic.
        elements = [i for i in range(len(self.clinic_demand))]
        probs = self.clinic_demand['prop'].to_numpy()
        self.clinic_dist = Discrete(elements, probs, random_seed=self.seeds[8])

        #create a list of clinic objects
        self.clinics = []
        for i in range(len(self.clinic_demand)):
            clinic = Clinic(self.clinic_demand['referred_out'].iloc[i],
                            random_seed=self.seeds[i+9])
            self.clinics.append(clinic)

    def create_carve_out(self, run_length, capacity_template):

        #proportion of total capacity carved out for high priority patients
        priority_template = (capacity_template * self.prop_carve_out).round().astype(np.uint8)

        priority_slots = priority_template.copy()

        #longer than run length as patients will need to book ahead
        for day in range(int(run_length*1.5)):
            priority_slots = pd.concat([priority_slots, priority_template.copy()],
                                        ignore_index=True)

        priority_slots.index.rename('day', inplace=True)
        return priority_slots

    def create_slots(self, run_length, capacity_template):

        priority_template = (capacity_template * self.prop_carve_out).round().astype(np.uint8)
        open_template = capacity_template - priority_template
        available_slots = open_template.copy()

        #longer than run length as patients will need to book ahead
        for day in range(int(run_length*1.5)):
            available_slots = pd.concat([available_slots, open_template.copy()],
                                         ignore_index=True)

        available_slots.index.rename('day', inplace=True)
        return available_slots

    def create_bookings(self, run_length, clinics):
        bookings = np.zeros(shape=(5, clinics), dtype=np.uint8)

        columns = [f'clinic_{i}' for i in range(1, clinics+1)]
        bookings_template = pd.DataFrame(bookings, columns=columns)

        bookings = bookings_template.copy()

        #longer than run length as patients will need to book ahead
        for day in range(int(run_length*1.5)):
            bookings = pd.concat([bookings, bookings_template.copy()],
                                 ignore_index=True)

        bookings.index.rename('day', inplace=True)
        return bookings

class LowPriorityPooledBooker():
    '''
    Low prioity booking process for POOLED clinics.

    Low priority patients only have access to public slots and have a minimum
    waiting time (e.g. 3 days before a slot can be used.)
    '''
    def __init__(self, args):
        self.args = args
        self.min_wait = LOW_PRIORITY_MIN_WAIT
        self.priority = 1


    def find_slot(self, t, clinic_id,
                  limit_clinic_choice = None):
        '''
        Finds a slot in a diary of available slot

        NUMPY IMPLEMENTATION.

        Params:
        ------
        t: int,
            time t in days

        clinic_id: int
            home clinic id is the index  of the clinic column in diary

        limit_clinic_choice: list
            mask (list of true false per clinic index) for clinics to allow
            additional filtering at time of booking over and above standard pooling

        Returns:
        -------
        (int, int)
        (best_t, best_clinic_id)

        '''
        #to reduce runtime - drop down to numpy...
        available_slots_np = self.args.available_slots.to_numpy()

        #get the clinics that are pooled with this one.
        # This is a leftover from when this model was clinic-level instead of
        # clinician level - however, it was much quicker to just set it up so that
        # for the initial appointment, everyone was pooled with everyone else using
        # the pooling file, rather than rewriting the model to change the logic of
        # patient arrivals. This is why this code and reference to pooling in places
        # might feel a bit strange - looking at ex_4_community will help you understand
        # why it's like this and why that option is very useful in other contexts.
        # In short - this works fine and it's not worth rewriting in this instance!
        clinic_options = np.where(self.args.pooling_np[clinic_id] == 1)[0]
        # Then mask further by those with availability
        clinic_options = clinic_options[limit_clinic_choice]

        #get the clinic slots t+min_wait forward for the pooled clinics
        clinic_slots = available_slots_np[t+self.min_wait:, clinic_options]

        #get the earliest day number (its the name of the series)
        best_t = np.where((clinic_slots.sum(axis=1) > 0))[0][0]

        #get the index of the best clinic option.
        # To ensure it's not always the first available clinician with availability
        # (as this can lead to odd behaviour with e.g. clinicians earlier in the list
        # getting all of the emergency patients when multiple clinicians have availability
        # on the same day)
        clinic_sample = random.randint(0, len(clinic_options[clinic_slots[best_t, :] > 0])-1)

        best_clinic_idx = clinic_options[clinic_slots[best_t, :] > 0][clinic_sample]

        #return (best_t, booked_clinic_id)
        return best_t + self.min_wait + t, best_clinic_idx


    def book_slot(self, booking_t, clinic_id):
        '''
        Book a slot on day t for clinic c

        A slot is removed from args.available_slots
        A appointment is recorded in args.bookings.iat

        Params:
        ------
        booking_t: int
            Day of booking

        clinic_id: int
            the clinic identifier
        '''
        #one less public available slot
        self.args.available_slots.iat[booking_t, clinic_id] -= 1

        #one more patient waiting
        self.args.bookings.iat[booking_t, clinic_id] += 1
class HighPriorityPooledBooker():
    '''
    High prioity booking process for POOLED clinics.

    High priority patients have access to public and reserved
    slots and have a minimum waiting time (e.g. 1 days before a
    slot can be used.)
    '''
    def __init__(self, args):
        self.args = args
        self.min_wait = 1
        self.priority = 2


    def find_slot(self, t,
                  clinic_id,
                  limit_clinic_choice = None):
        '''
        Finds a slot in a diary of available slot

        NUMPY IMPLEMENTATION.

        Params:
        ------
        t: int,
            time t in days

        clinic_id: int
            home clinic id is the index  of the clinic column in diary

        limit_clinic_choice: list
            mask (list of true false per clinic index) for clinics to allow
            additional filtering at time of booking over and above standard pooling

        Returns:
        -------
        (int, int)
        (best_t, best_clinic_id)

        '''
        #to reduce runtime - drop down to numpy...
        available_slots_np = self.args.available_slots.to_numpy()
        carve_out_slots_np = self.args.carve_out_slots.to_numpy()

        #get the clinics that are pooled with this one.
        clinic_options = np.where(self.args.pooling_np[clinic_id] == 1)[0]
        # Then mask further by those with availability
        clinic_options = clinic_options[limit_clinic_choice]

        #get the clinic slots t+min_wait forward for the pooled clinics
        public_slots = available_slots_np[t+self.min_wait:, clinic_options]
        priority_slots = carve_out_slots_np[t+self.min_wait:, clinic_options]

        #total slots
        clinic_slots = priority_slots + public_slots

        #get the earliest day number (its the name of the series)
        best_t = np.where((clinic_slots.sum(axis=1) > 0))[0][0]

        #get the index of the best clinic option.
        # To ensure it's not always the first available clinician with availability
        # (as this can lead to odd behaviour with e.g. clinicians earlier in the list
        # getting all of the emergency patients when multiple clinicians have availability
        # on the same day)
        clinic_sample = random.randint(0, len(clinic_options[clinic_slots[best_t, :] > 0])-1)

        best_clinic_idx = clinic_options[clinic_slots[best_t, :] > 0][clinic_sample]

        #return (best_t, best_clinic_id)
        return best_t + self.min_wait + t, best_clinic_idx


    def book_slot(self, booking_t, clinic_id):
        '''
        Book a slot on day t for clinic c

        A slot is removed from args.available_slots
        A appointment is recorded in args.bookings.iat

        Params:
        ------
        booking_t: int
            Day of booking

        clinic_id: int
            the clinic identifier
        '''
        #take carve out slot first
        if self.args.carve_out_slots.iat[booking_t, clinic_id] > 0:
            self.args.carve_out_slots.iat[booking_t, clinic_id] -= 1
        else:
            #one less public available slot
            self.args.available_slots.iat[booking_t, clinic_id] -= 1

        #one more booking...
        self.args.bookings.iat[booking_t, clinic_id] += 1

class RepeatBooker():
    '''
    Repeat Booking for clients who need to be seen at a high frequency
    (weekly)

    Set the minimum wait to be one day fewer

    clinic_id: int
        the clinic identifier
    '''
    def __init__(self, args, ideal_frequency, clinic_id):
        self.args = args
        self.ideal_frequency = ideal_frequency
        self.clinic_id = clinic_id
        # Set minimum wait to 1 day fewer than stated ideal frequency
        self.min_wait = ideal_frequency - 1
        self.priority = 1

    def find_slot(self, t):
        '''
        Finds a slot in a diary of available slot

        Params:
        ------
        t: int,
            time t in days

        clinic_id: int
            clinic id is the index  of the clinic column in diary

        Returns:
        -------
        (int, int)
        (best_t, best_clinic_id)
        '''
        #to reduce runtime drop from pandas to numpy
        available_slots_np = self.args.available_slots.to_numpy()

        #get the clinic slots t+min_wait forward for the pooled clinics
        clinic_slots = available_slots_np[t+self.min_wait:, self.clinic_id]

        # return (best_t, best_clinic_id)
        return np.argmax(clinic_slots > 0) + self.min_wait + t, self.clinic_id


    def book_slot(self, booking_t):
        '''
        Book a slot on day t for clinic c

        A slot is removed from args.available_slots
        A appointment is recorded in args.bookings.iat

        Params:
        ------
        booking_t: int
            Day of booking
        '''
        #one less public available slot
        self.args.available_slots.iat[booking_t, self.clinic_id] -= 1

        #one more patient waiting
        self.args.bookings.iat[booking_t, self.clinic_id] += 1




class PatientReferral(object):
    '''
    Patient referral process

    Find an appropraite asessment slot for the patient.
    Schedule an assessment for that day.

    '''
    def __init__(self, env, args, referral_t, home_clinic,
                 booker,
                 event_log, identifier):
        self.env = env
        self.args = args
        self.referral_t = referral_t
        self.home_clinic = home_clinic
        self.booked_clinic = home_clinic

        self.booker = booker

        self.event_log = event_log
        self.identifier = identifier

        #performance metrics
        self.waiting_time = None

        self.follow_up_intensity = None

    @property
    def priority(self):
        '''
        Return the priority of the patient booking
        '''
        return self.booker.priority

    def execute(self):
        '''
        Patient is referred to clinic

        1. find earliest slot within rules
        2. book slot at clinic
        3. schedule process to complete at that time
        '''
        self.event_log.append(
            {'patient': self.identifier,
             'pathway': self.priority,
             'event_type': 'arrival_departure',
             'event': 'arrival',
             'home_clinic': int(self.home_clinic),
             'time': self.env.now}
        )

        self.event_log.append(
                {'patient': self.identifier,
                'pathway': self.priority,
                'event_type': 'queue',
                'event': 'waiting_appointment_to_be_scheduled',
                'home_clinic': int(self.home_clinic),
                'time': self.env.now
                }
            )

        # If priority is high, just book next available appointment
        # regardless of how far in the future it is

        if self.priority == 2:
            #get slot for clinic
            best_t, self.booked_clinic = \
                self.booker.find_slot(self.referral_t, self.home_clinic)

            #book slot at clinic = time of referral + waiting_time
            self.booker.book_slot(best_t, self.booked_clinic)

            self.event_log.append(
                {'patient': self.identifier,
                'pathway': self.priority,
                'event_type': 'queue',
                'event': 'appointment_booked_waiting',
                'booked_clinic': int(self.booked_clinic),
                'home_clinic': int(self.home_clinic),
                'time': self.env.now
                }
            )

        # if priority is low, check whether an appointment is available in the next six weeks
        # if it is not, wait one day, and then check again
        # This is to try and prevent the books becoming overfull, leading to gaps that are too long
        # Between regular appointments
        # But need to make sure people in the booking queue get checked again before any new arrivals
        if self.priority == 1:
            #get slot for clinic

            # This is their first time trying to get an appointment
            # Delay their search slightly to ensure they don't get an appointment ahead of someone
            # who has been in the queue for longer (who will be checking daily for an available appointment)
            # becoming available that meets the wait rules
            # yield self.env.timeout(0.1)

            # First, wait one day so you don't leapfrog ahead of anyone who has been waiting for a while
            # THIS IS NOT AN IDEAL WORKAROUND - BUT CAN'T GO INTO PARTIAL SIMULATION TIME UNITS

            yield self.env.timeout(1)
            # TO ACCOUNT FOR WORKAROUND, ADJUST THE MINIMUM WAIT HERE
            self.booker.min_wait = self.booker.min_wait - 1

            # First calculate the caseload of each clinician
            # Caseload calculation is based on the number of slots they have available
            # each week, the number of high intensity patients they have (take up 1 slot per week,
            # so 1 caseload slot), and the number of low intensity caseload patients they have (take
            # up 1 slot every other week, so 0.5 caseload slots). Want to leave a buffer of 1 caseload
            # slot per clinician (i.e. if they have 15 theoretical slots per week but 14 are already taken,
            # we will count this as full for these purposes - as this leaves some flexibility for
            # high priority/urgent patients, who will bypass the check and be admitted to caseload anyway)

            # What we need to check is the number of people currently booked for assessment or on the books
            # with each clinician
            # this is stored in self.args.existing_caseload

            def check_for_availability():
                # Then we calculate their theoretical maximum from the slots file
                caseload_slots_per_clinician = (self.args.weekly_slots).sum().to_numpy().T
                # Then we subtract one from the other to get the available slots
                # Then subtract one from the theoretical maximum because we want to leave headroom
                # for emergency clients
                available_caseload = (caseload_slots_per_clinician - self.args.existing_caseload.tolist()[1:])- 1
                clinicians_with_slots = len([c for c in available_caseload if c > 0])
                return clinicians_with_slots, available_caseload

            # Do an initial check for if anyone has capacity
            # and if they do, check who has the soonest appointment
            # If no-one has capacity, time out and wait until tomorrow instead
            # when a fresh check will be done.
            clinicians_with_slots, available_caseload = check_for_availability()

            if clinicians_with_slots > 0:
                print(f"Clinicians with slots: {clinicians_with_slots}")
                best_t, self.booked_clinic = \
                    self.booker.find_slot(self.referral_t,
                                        self.home_clinic,
                                        limit_clinic_choice = [True if c > 0 else False
                                                               for c
                                                               in available_caseload]
                                        )

            else:
                # TO ACCOUNT FOR WORKAROUND, ADJUST THE MINIMUM WAIT BACK TO THE ORIGINAL LENGTH HERE
                self.booker.min_wait = self.booker.min_wait + 1
                # As there are no slots available if we've reached this point of the code, let's wait
                # until the next day
                yield self.env.timeout(1)
                # Now check if anyone has left the caseload since yesterday and if anyone now has availability
                clinicians_with_slots, available_caseload = check_for_availability()
                # Continue to check this until someone has availability
                while clinicians_with_slots == 0:
                    print(f"client {self.identifier} at {self.env.now} found no availability")
                    yield self.env.timeout(1)
                    # Recheck availability
                    clinicians_with_slots, available_caseload = check_for_availability()

                # Once loop escaped, we know there's availability, so find that slot
                best_t, self.booked_clinic = \
                    self.booker.find_slot(self.env.now, self.home_clinic,
                                    limit_clinic_choice = [True if c>0 else False
                                                           for c
                                                           in available_caseload])

            #book slot at clinic = time of referral + waiting_time
            self.booker.book_slot(best_t, self.booked_clinic)
            print(f"client {self.identifier} seized booking at {self.env.now}")

            self.event_log.append(
                {'patient': self.identifier,
                'pathway': self.priority,
                'event_type': 'queue',
                'event': 'appointment_booked_waiting',
                'booked_clinic': int(self.booked_clinic),
                'home_clinic': int(self.home_clinic),
                'time': self.env.now
                }
            )

            # Once slot is booked, client is now on caseload for that clinician,
            # so add to the caseload file
            # if high priority, likelihood is that they will go on to high intensity
            # so one caseload slot
            if self.priority == 2:
                self.args.existing_caseload[1:].iloc[int(self.booked_clinic)] += 1
            else:
                self.args.existing_caseload[1:].iloc[int(self.booked_clinic)] += 1

        #wait for appointment
        yield self.env.timeout(best_t - self.referral_t)

        # measure waiting time on day of appointment
        #(could also record this before appointment, but leaving until
        #afterwards allows modifications where patients can be moved)
        self.waiting_time = best_t - self.referral_t

        # Use appointment
        self.event_log.append(
            {'patient': self.identifier,
             'pathway': self.priority,
             'event_type': 'queue',
             'event': 'have_appointment',
             'booked_clinic': int(self.booked_clinic),
             'home_clinic': int(self.home_clinic),
             'type': "assessment",
             'time': self.env.now,
             'wait': self.waiting_time
             }
        )

        # First sample whether they will have any follow-up appointments
        # If low priority
        if int(self.priority) == 1:
            follow_up_y = self.args.follow_up_dist_low_priority.sample()
        elif int(self.priority) == 2:
            follow_up_y = self.args.follow_up_dist_high_priority.sample()
        else:
            print(f"Error - Unknown priority value received ({self.priority})")

        # print(follow_up_y)

        # Sample whether they will need high-intensity follow-up
        # (every 7 days) or low-intensity follow-up (every 21 days)
        if follow_up_y:
            # self.event_log.append(
            #         {'patient': self.identifier,
            #         'pathway': self.priority,
            #         'event_type': 'attribute',
            #         'event': 'follow_ups_required',
            #         'booked_clinic': int(self.booked_clinic),
            #         'home_clinic': int(self.home_clinic),
            #         'time': self.env.now
            #         }
            #     )
            if int(self.priority) == 1:
                self.follow_up_intensity = self.args.intensity_dist_low_priority.sample()
            elif int(self.priority) == 2:
                self.follow_up_intensity = self.args.intensity_dist_high_priority.sample()
            else:
                print(f"Error - Unknown priority value received ({self.priority})")

            # Adjust caseload values if expected pathway not follows
            # if high priority has low intensity follow up then we need to reduce the
            # number of caseload slots they are using from 1 to 0.5:
            if self.follow_up_intensity == 0 and int(self.priority) == 2:
                self.args.existing_caseload[1:].iloc[int(self.booked_clinic)] -= 0.5
            # if low priority has high intensity follow up then we need to up
            # the number of caseload slots they are using from 0.5 to 1:
            if self.follow_up_intensity == 1 and int(self.priority) == 1:
                self.args.existing_caseload[1:].iloc[int(self.booked_clinic)] += 0.5

            # Now sample how many follow-up appointments they need
            if self.follow_up_intensity == 1:
                num_appts = int(self.args.num_follow_up_dist_high_intensity.sample())
                repeat_booker = RepeatBooker(
                    ideal_frequency=HIGH_INTENSITY_FOLLOW_UP_TARGET_INTERVAL,
                    args = self.args,
                    clinic_id=self.booked_clinic)
            else:
                num_appts = int(self.args.num_follow_up_dist_low_intensity.sample())
                repeat_booker = RepeatBooker(
                    args = self.args,
                    ideal_frequency=LOW_INTENSITY_FOLLOW_UP_TARGET_INTERVAL,
                    clinic_id=self.booked_clinic)

            for i in range(num_appts):
                best_t, clinic = \
                    repeat_booker.find_slot(self.env.now)

                #book slot at clinic = time of referral + waiting_time
                repeat_booker.book_slot(best_t)

                self.event_log.append(
                    {'patient': self.identifier,
                    'pathway': self.priority,
                    'event_type': 'queue',
                    'event': 'follow_up_appointment_booked_waiting',
                    'booked_clinic': int(self.booked_clinic),
                    'home_clinic': int(self.home_clinic),
                    'follow_up': i,
                    'follow_up_intensity': 'high' if self.follow_up_intensity == 1 else 'low',
                    'follow_ups_intended': num_appts,
                    'time': self.env.now
                    }
                )

                interval = best_t - self.env.now

                #wait for appointment
                yield self.env.timeout(best_t - self.env.now)

                # Use appointment
                self.event_log.append(
                    {'patient': self.identifier,
                    'pathway': self.priority,
                    'event_type': 'queue',
                    'event': 'have_appointment',
                    'booked_clinic': int(self.booked_clinic),
                    'home_clinic': int(self.home_clinic),
                    'time': self.env.now,
                    'type': "follow-up",
                    'follow_up': i,
                    'follow_up_intensity': 'high' if self.follow_up_intensity == 1 else 'low',
                    'follow_ups_intended': num_appts,
                    'interval': interval
                    }
                )

                i += 1

                # Repeat this loop until all predefined appointments have taken place
        # else:
        #                 self.event_log.append(
        #         {'patient': self.identifier,
        #         'pathway': self.priority,
        #         'event_type': 'attribute',
        #         'event': 'follow_ups_not_required',
        #         'booked_clinic': int(self.booked_clinic),
        #         'home_clinic': int(self.home_clinic),
        #         'time': self.env.now
        #         }
        #     )

        # Once they reach this part of the code, they are leaving the system, so can
        # be removed from the caseload file
        if self.follow_up_intensity == 1:
            self.args.existing_caseload[1:].iloc[int(self.booked_clinic)] -= 1
        elif self.follow_up_intensity == 0:
            self.args.existing_caseload[1:].iloc[int(self.booked_clinic)] -= 0.5

        self.event_log.append(
            {'patient': self.identifier,
             'pathway': self.priority,
             'event_type': 'arrival_departure',
             'event': 'depart',
             'home_clinic': int(self.home_clinic),
             'time': self.env.now+1}
        )



class AssessmentReferralModel(object):
    '''
    Implements the Mental Wellbeing and Access 'Assessment Referral'
    model in Pitt, Monks and Allen (2015). https://bit.ly/3j8OH6y

    Patients arrive at random and in proportion to the regional team.

    Patients may be seen by any team identified by a pooling matrix.
    This includes limiting a patient to only be seen by their local team.

    The model reports average waiting time and can be used to compare
    full, partial and no pooling of appointments.

    '''
    def __init__(self, args):
        '''
        Constructor

        Params:
        ------

        args: Scenario
            Arguments for the simulation model

        '''
        self.env = simpy.Environment()
        self.args = args

        #list of patients referral processes
        self.referrals = []

        self.event_log = []

        #simpy processes
        self.env.process(self.generate_arrivals())

    def run(self):
        '''
        Conduct a single run of the simulation model.
        '''
        self.env.run(self.args.run_length)
        self.process_run_results()

    def generate_arrivals(self):
        '''
        Time slicing simulation.  The model steps forward by a single
        day and simulates the number of arrivals from a Poisson
        distribution.  The following process is then applied.

        1. Sample the region of the referral from a Poisson distribution
        2. Triage - is an appointment made for the patient or are they referred
        to another service?
        3. A referral process is initiated for the patient.

        '''
        #loop a day at a time.
        for t in itertools.count():

            #total number of referrals today
            n_referrals = self.args.arrival_dist.sample()

            #loop through all referrals recieved that day
            for i in range(n_referrals):
                #sample clinic based on empirical proportions
                clinic_id = self.args.clinic_dist.sample()
                clinic = self.args.clinics[clinic_id]

                #triage patient and refer out of system if appropraite
                referred_out = clinic.ref_out_dist.sample()

                #if patient is accepted to clinic
                if referred_out == 0:

                    #is patient high priority?
                    high_priority = self.args.priority_dist.sample()

                    if high_priority == 1:
                        assessment_booker = HighPriorityPooledBooker(self.args)
                    else:
                        assessment_booker = LowPriorityPooledBooker(self.args)

                    #create instance of PatientReferral
                    patient = PatientReferral(self.env, self.args,
                                              referral_t=t,
                                              home_clinic=clinic_id,
                                              booker=assessment_booker,
                                              event_log=self.event_log,
                                              identifier=f"{t}_{i}")

                    #start a referral assessment process for patient.
                    self.env.process(patient.execute())

                    #only collect results after warm-up complete
                    if self.env.now > self.args.warm_up_period:
                        #store patient for calculating waiting time stats at end
                        self.referrals.append(patient)

                # Add event logging for patients triaged and referred out
                if referred_out == 1:
                    self.event_log.append(
                        {'patient': f"{t}_{i}",
                        'pathway': "Unsuitable for service",
                        'event_type': 'arrival_departure',
                        'event': 'arrival',
                        'home_clinic': int(clinic_id),
                        'time': self.env.now
                        }
                    )

                    self.event_log.append(
                        {'patient': f"{t}_{i}",
                        'pathway': "Unsuitable for service",
                        'event_type': 'queue',
                        'event': 'referred_out',
                        'home_clinic': int(clinic_id),
                        'time': self.env.now
                        }
                    )

                    self.event_log.append(
                        {'patient': f"{t}_{i}",
                        'pathway': "Unsuitable for service",
                        'event_type': 'arrival_departure',
                        'event': 'depart',
                        'home_clinic': int(clinic_id),
                        'time': self.env.now + 1
                        }
                    )

            #timestep by one day
            yield self.env.timeout(1)

    def process_run_results(self):
        '''
        Produce summary results split by priority...
        '''

        results_all = [p.waiting_time for p in self.referrals
               if not p.waiting_time is None]

        results_low = [p.waiting_time for p in self.referrals
                       if not (p.waiting_time is None) and p.priority == 1]

        results_high = [p.waiting_time for p in self.referrals
                       if (not p.waiting_time is None) and p.priority == 2]

        self.results_all = results_all
        self.results_low = results_low
        self.results_high = results_high